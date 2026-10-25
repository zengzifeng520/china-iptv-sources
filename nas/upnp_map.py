#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upnp_map.py —— 通过 UPnP 让路由器自动做端口映射（8898 -> NAS:8898）"""
import re
import socket
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SSDP_ADDR = ("239.255.255.250", 1900)
ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"


def discover(timeout=3):
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           "ST: %s\r\n\r\n" % ST).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        s.sendto(msg, SSDP_ADDR)
    except OSError as e:
        print("SSDP 发送失败:", e)
        return []
    found = []
    while True:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            break
        text = data.decode("utf-8", "replace")
        m = re.search(r"LOCATION:\s*(\S+)", text, re.I)
        if m:
            found.append((addr[0], m.group(1)))
    s.close()
    return found


def get_xml(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def find_control(xml, base):
    # 找 WANIPConnection / WANPPPConnection 的 controlURL
    for svc in re.findall(r"<service>(.*?)</service>", xml, re.S):
        st = re.search(r"<serviceType>(.*?)</serviceType>", svc, re.S)
        cu = re.search(r"<controlURL>(.*?)</controlURL>", svc, re.S)
        if st and cu and "WAN" in st.group(1) and "Connection" in st.group(1):
            u = cu.group(1).strip()
            if u.startswith("http"):
                return u
            if u.startswith("/"):
                m = re.match(r"(https?://[^/]+)", base)
                return m.group(1) + u
            return base.rsplit("/", 1)[0] + "/" + u
    return None


def soap(control, action, body, service):
    env = ('<?xml version="1.0"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
           '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
           % (action, service, body, action)).encode()
    req = urllib.request.Request(control, data=env, headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"%s#%s"' % (service, action),
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def main():
    ext_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    int_port = int(sys.argv[2]) if len(sys.argv) > 2 else 8898
    int_ip = sys.argv[3] if len(sys.argv) > 3 else "192.168.3.21"

    devs = discover()
    print("发现 UPnP 设备:", devs)
    for ip, loc in devs:
        try:
            xml = get_xml(loc)
        except Exception as e:  # noqa: BLE001
            print("  取设备描述失败", loc, e)
            continue
        base = loc
        ctrl = find_control(xml, base)
        if not ctrl:
            continue
        print("  控制地址:", ctrl)
        svc = "urn:schemas-upnp-org:service:WANIPConnection:1"
        if "WANPPPConnection" in xml and "WANIPConnection" not in xml:
            svc = "urn:schemas-upnp-org:service:WANPPPConnection:1"
        try:
            r = soap(ctrl, "GetExternalIPAddress", "", svc)
            m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", r)
            print("  路由器当前外网 IP:", m.group(1) if m else "?")
        except Exception as e:  # noqa: BLE001
            print("  GetExternalIPAddress 失败:", e)
        body = ("<NewRemoteHost></NewRemoteHost>"
                "<NewExternalPort>%d</NewExternalPort>"
                "<NewProtocol>TCP</NewProtocol>"
                "<NewInternalPort>%d</NewInternalPort>"
                "<NewInternalClient>%s</NewInternalClient>"
                "<NewEnabled>1</NewEnabled>"
                "<NewPortMappingDescription>iptv-track</NewPortMappingDescription>"
                "<NewLeaseDuration>0</NewLeaseDuration>") % (ext_port, int_port, int_ip)
        try:
            soap(ctrl, "AddPortMapping", body, svc)
            print("  ✅ 端口映射已添加: 外网 %d -> %s:%d (TCP)" % (ext_port, int_ip, int_port))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if "718" in detail:
                print("  ⚠️ 冲突 718：该端口已有别的映射")
            elif "724" in detail:
                print("  ⚠️ 724：找不到内网主机")
            else:
                print("  ❌ 添加失败:", e, detail)
        except Exception as e:  # noqa: BLE001
            print("  ❌ 添加失败:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
