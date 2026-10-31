# -*- coding: utf-8 -*-
"""让内嵌浏览器（WebView2）信任 EVE 的自签证书。

新版 EVE Pro 的 Web 服务跑在 443 上是自签证书。桌面窗口里的「图形控制台」是
一个 <iframe>，走的是 WebView2 自己的网络栈（不经过本程序的 Python 客户端，
那边本来就不校验证书），所以证书不被信任时只会得到一张
NET::ERR_CERT_AUTHORITY_INVALID 的错误页 —— 看起来就是「网页打不开」。

这里**不做**全局 --ignore-certificate-errors（等于把整个内嵌浏览器的证书校验
关掉）。改用 Chromium 的 --ignore-certificate-errors-spki-list：只放行这台
服务器当前用的那张证书（按公钥指纹）。证书换了（重装 / 续签）下次启动会重新
算，白名单跟着变；别的站点仍然正常校验。

环境变量必须在 WebView2 创建浏览器环境之前设置，也就是 webview.start() 之前，
所以由 app.py 在启动时调用 apply_from_config()。
"""
import base64
import hashlib
import os
import socket
import ssl

ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
FLAG = "--ignore-certificate-errors-spki-list="

_cached = {}


def spki_hash(host, port, timeout=3.0):
    """取服务器证书的公钥指纹（Chromium 用的 base64(SHA256(SPKI))）。

    连不上、证书读不出来都返回 None —— 只是没有白名单，不影响别的功能。
    """
    key = (host, int(port))
    if key in _cached:
        return _cached[key]
    h = None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                der = s.getpeercert(binary_form=True)
        if der:
            h = _spki_from_der(der)
    except Exception:
        h = None
    _cached[key] = h
    return h


def _spki_from_der(der):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_der_x509_certificate(der)
        spki = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception:
        # 没有 cryptography（正常安装有，paramiko 依赖它）时退回解析 DER 头
        spki = _spki_raw(der)
        if not spki:
            return None
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def _spki_raw(der):
    """极简 ASN.1 兜底：Certificate → tbsCertificate → 第 6 个字段 SPKI。

    只在 cryptography 不可用时走这里，按 RFC 5280 的固定字段顺序取；
    遇到不认识的编码就返回 None（宁可没有白名单，也不要算错指纹）。
    """
    def read(buf, i):
        """返回 (tag, 头起点, 内容起点, 内容长度)。"""
        if i + 2 > len(buf):
            return None
        tag, ln, hdr = buf[i], buf[i + 1], i
        i += 2
        if ln & 0x80:
            n = ln & 0x7F
            if n == 0 or n > 4 or i + n > len(buf):
                return None
            ln = int.from_bytes(buf[i:i + n], "big")
            i += n
        if i + ln > len(buf):
            return None
        return tag, hdr, i, ln

    try:
        top = read(der, 0)                     # Certificate (SEQUENCE)
        if not top or top[0] != 0x30:
            return None
        tbs = read(der, top[2])                # tbsCertificate (SEQUENCE)
        if not tbs or tbs[0] != 0x30:
            return None
        j = tbs[2]
        if j < len(der) and der[j] == 0xA0:    # [0] version，可选
            v = read(der, j)
            if not v:
                return None
            j = v[2] + v[3]
        for _ in range(5):                     # serial, sigalg, issuer, validity, subject
            f = read(der, j)
            if not f:
                return None
            j = f[2] + f[3]
        spki = read(der, j)                    # subjectPublicKeyInfo
        if not spki or spki[0] != 0x30:
            return None
        return der[spki[1]:spki[2] + spki[3]]
    except Exception:
        return None


def current_hashes():
    """已经写进环境变量的白名单（用于判断某台机器的证书是否已放行）。"""
    val = os.environ.get(ENV, "")
    if FLAG not in val:
        return []
    tail = val.split(FLAG, 1)[1].strip().split(" ")[0]
    return [x for x in tail.split(",") if x]


def apply(hashes):
    """把指纹塞进 WebView2 的启动参数。已有同名参数（用户自己配的）就不覆盖。"""
    hashes = [h for h in dict.fromkeys(hashes or []) if h]
    if not hashes:
        return False
    cur = os.environ.get(ENV, "").strip()
    if FLAG in cur:
        return True
    os.environ[ENV] = (cur + " " + FLAG + ",".join(hashes)).strip()
    return True


def apply_from_config(cfg):
    """启动时调用：把配置里所有 HTTPS 服务器的证书都放行。

    返回 (放行了几个, 说明文字)。任何异常都不该阻止程序启动。
    """
    profs = []
    try:
        profs = cfg.data.get("profiles") or []
    except Exception:
        return 0, "读不到配置"
    hashes = []
    for p in profs:
        scheme = (p.get("scheme") or "").strip().lower()
        try:
            port = int(p.get("api_port") or 0)
        except Exception:
            port = 0
        if scheme != "https" and port not in (443, 8443):
            continue
        host = (p.get("host") or "").strip()
        if not host:
            continue
        stored = (p.get("cert_spki") or "").strip()
        h = stored or spki_hash(host, port)
        if h:
            hashes.append(h)
    if not hashes:
        return 0, "没有 HTTPS 服务器"
    apply(hashes)
    return len(hashes), "已放行 %d 张自签证书，图形控制台可用" % len(hashes)
