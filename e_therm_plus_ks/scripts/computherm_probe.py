#!/usr/bin/env python3
"""
Local diagnostic for Computherm WebForms synoptic pages.

It intentionally keeps credentials in a local ignored JSON file:
  computherm_secrets.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECRET_PATH = ROOT / "computherm_secrets.json"


@dataclass
class HttpResponse:
    url: str
    text: str


class HttpClient:
    def __init__(self) -> None:
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    def get(self, url: str, timeout: int = 30) -> HttpResponse:
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with self.opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            final_url = resp.geturl()
            charset = resp.headers.get_content_charset() or "utf-8"
        return HttpResponse(final_url, data.decode(charset, errors="replace"))

    def post(self, url: str, data: dict[str, str], timeout: int = 30) -> HttpResponse:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        headers = dict(self.headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
        with self.opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            final_url = resp.geturl()
            charset = resp.headers.get_content_charset() or "utf-8"
        return HttpResponse(final_url, body.decode(charset, errors="replace"))


class InputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {str(k).lower(): "" if v is None else str(v) for k, v in attrs}
        if tag.lower() == "input":
            self.inputs.append(data)
        elif tag.lower() == "form":
            self.forms.append(data)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        sample = {
            "login_url": "https://servizi.computherm.it/...",
            "username": "INSERISCI_UTENTE",
            "password": "INSERISCI_PASSWORD",
            "username_field": "",
            "password_field": "",
            "login_button_field": "",
            "login_button_value": "",
            "refresh_button_name": "ctl00$cph_body$ibtn_read_io_1",
            "dashboards": [
                {
                    "id": "ct",
                    "name": "CT",
                    "url": "https://servizi.computherm.it/WebForms/Service/WebDevManager/Synoptic?P=...&S=...",
                },
                {
                    "id": "subct_1",
                    "name": "SUBCT 1",
                    "url": "https://servizi.computherm.it/WebForms/Service/WebDevManager/Synoptic?P=...&S=...",
                },
                {
                    "id": "subct_2",
                    "name": "SUBCT 2",
                    "url": "https://servizi.computherm.it/WebForms/Service/WebDevManager/Synoptic?P=...&S=...",
                },
            ],
        }
        path.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(f"Creato {path}. Compilalo localmente e rilancia lo script.")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_inputs(html: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    parser = InputParser()
    parser.feed(html)
    return parser.inputs, parser.forms


def form_payload_from_html(html: str) -> dict[str, str]:
    inputs, _forms = parse_inputs(html)
    payload: dict[str, str] = {}
    for item in inputs:
        name = item.get("name") or ""
        if not name:
            continue
        typ = (item.get("type") or "").lower()
        if typ in ("submit", "button", "image", "file"):
            continue
        payload[name] = item.get("value", "")
    return payload


def print_form_summary(html: str) -> None:
    inputs, forms = parse_inputs(html)
    print("Forms trovati:")
    for idx, form in enumerate(forms, start=1):
        print(f"  {idx}. action={form.get('action', '')} method={form.get('method', '')}")
    print("Input utili trovati:")
    for item in inputs:
        name = item.get("name") or item.get("id") or ""
        typ = item.get("type") or ""
        if name and typ.lower() not in ("hidden",):
            print(f"  type={typ:<12} name={name} id={item.get('id', '')}")


def login(session: HttpClient, cfg: dict[str, Any]) -> None:
    login_url = str(cfg.get("login_url") or "").strip()
    if not login_url or login_url.endswith("/..."):
        raise SystemExit("Configura login_url in computherm_secrets.json.")

    response = session.get(login_url, timeout=30)
    login_html = response.text

    username_field = str(cfg.get("username_field") or "").strip()
    password_field = str(cfg.get("password_field") or "").strip()
    if not username_field or not password_field:
        print("Campi login non configurati. Ecco cosa ho trovato nella pagina login:")
        print_form_summary(login_html)
        raise SystemExit("Compila username_field/password_field nel JSON e rilancia.")

    payload = form_payload_from_html(login_html)
    payload[username_field] = str(cfg.get("username") or "")
    payload[password_field] = str(cfg.get("password") or "")

    button_field = str(cfg.get("login_button_field") or "").strip()
    if button_field:
        payload[button_field] = str(cfg.get("login_button_value") or "Accedi")

    inputs, forms = parse_inputs(login_html)
    action = forms[0].get("action", "") if forms else ""
    post_url = urljoin(login_url, action) if action else login_url
    result = session.post(post_url, payload, timeout=30)

    if "password" in result.text.lower() and "login" in result.url.lower():
        print("Login forse NON riuscito: la risposta sembra ancora una pagina login.")
    else:
        print(f"Login tentato. URL finale: {result.url}")


def extract_js_array(html: str, name: str) -> list[Any]:
    match = re.search(rf"var\s+{re.escape(name)}\s*=\s*new\s+Array\s*\((.*?)\);", html, flags=re.S)
    if not match:
        return []
    raw = match.group(1).strip()
    if raw == "null":
        return []
    return json.loads(raw)


def extract_csprobes(html: str) -> list[dict[str, Any]]:
    for name in ("CSSensors", "CSProbes"):
        items = extract_js_array(html, name)
        if items:
            return [x for x in items if isinstance(x, dict)]
    return []


def page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def print_no_probe_diagnostics(name: str, stage: str, response: HttpResponse) -> None:
    html = response.text
    markers = []
    for marker in ("CSSensors", "CSProbes", "SinId", "Login", "Accesso", "UserServices", "Synoptic", "Non autorizzato", "Errore"):
        if marker.lower() in html.lower():
            markers.append(marker)
    print(
        f"[{name}] diagnostica {stage}: url_finale={response.url} "
        f"title={page_title(html)!r} len={len(html)} markers={','.join(markers) or '-'}"
    )


def refresh_synoptic(session: HttpClient, url: str, html: str, button_name: str) -> str:
    payload = form_payload_from_html(html)
    payload[f"{button_name}.x"] = "32"
    payload[f"{button_name}.y"] = "32"
    result = session.post(url, payload, timeout=45)
    return result.text


def probe_dashboard(session: HttpClient, dashboard: dict[str, Any], refresh_button_name: str) -> None:
    name = str(dashboard.get("name") or dashboard.get("id") or "dashboard")
    url = str(dashboard.get("url") or "").strip()
    if not url or "P=..." in url:
        print(f"[{name}] URL non configurato, salto.")
        return

    response = session.get(url, timeout=30)
    html = response.text

    probes = extract_csprobes(html)
    print(f"[{name}] GET: {len(probes)} sonde trovate.")
    if not probes:
        print_no_probe_diagnostics(name, "GET", response)

    try:
        refreshed_response = session.post(
            url,
            {
                **form_payload_from_html(html),
                f"{refresh_button_name}.x": "32",
                f"{refresh_button_name}.y": "32",
            },
            timeout=45,
        )
        refreshed = refreshed_response.text
        refreshed_probes = extract_csprobes(refreshed)
        if refreshed_probes:
            probes = refreshed_probes
        print(f"[{name}] refresh I/O: {len(refreshed_probes)} sonde trovate.")
        if not refreshed_probes:
            print_no_probe_diagnostics(name, "refresh", refreshed_response)
    except Exception as exc:
        print(f"[{name}] refresh I/O fallito: {exc}")

    for probe in probes:
        label = str(probe.get("Label") or "").replace("\r", " ").replace("\n", " ").strip()
        value = probe.get("Value")
        udm = str(probe.get("UDM") or "").replace("Â°", "°").replace("�", "°")
        if udm == "°":
            udm = "deg"
        sin_id = probe.get("SinId")
        if label:
            print(f"  {sin_id}: {label} = {value} {udm}".rstrip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_SECRET_PATH), help="Path JSON credenziali/config locale")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    session = HttpClient()

    login(session, cfg)
    refresh_button_name = str(cfg.get("refresh_button_name") or "ctl00$cph_body$ibtn_read_io_1")
    dashboards = cfg.get("dashboards") if isinstance(cfg.get("dashboards"), list) else []
    for dashboard in dashboards:
        if isinstance(dashboard, dict):
            probe_dashboard(session, dashboard, refresh_button_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
