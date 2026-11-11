"""Публичные страницы: скачивание Happ по ОС и добавление подписки в Happ.

Почему нужны страницы-редиректы: Telegram не принимает ссылки вида happ://... в кнопках
(допустимы только http(s) и tg://), поэтому кнопка ведёт на https-страницу, а она уже
открывает Happ через deep link и подсказывает запасные варианты.
"""
from __future__ import annotations

import html
import json

# Ссылки на загрузку Happ (официальный список: https://www.happ.su/main/readme).
# Если Happ поменяет адреса — правим только здесь.
LINKS = {
    "ios_ru": "https://apps.apple.com/ru/app/happ-lite/id6799917773",
    "ios_global": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
    "android": "https://play.google.com/store/apps/details?id=com.happproxy",
    "android_apk": "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk",
    "windows": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
    "windows_arm": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.arm64.exe",
    "macos_ru": "https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643",
    "macos_global": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
    "macos_dmg": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg",
    "linux": "https://github.com/Happ-proxy/happ-desktop/releases/latest",
    "all_releases": "https://github.com/Happ-proxy/happ-desktop/releases/latest",
}

GITHUB_FALLBACK_KEY = "all_releases"


def effective_links(dead: "set[str] | frozenset[str] | list[str] | tuple[str, ...]" = ()) -> dict[str, str]:
    """Ссылки для страницы: если прямая ссылка на файл GitHub перестала работать (переименовали ассет),
    подставляем страницу релизов — человек всё равно сможет скачать приложение."""
    out = dict(LINKS)
    for key in dead:
        url = out.get(key, "")
        if "github.com" in url and "/download/" in url:
            out[key] = LINKS[GITHUB_FALLBACK_KEY]
    return out


_CSS = """
:root{color-scheme:light dark;--bg:#f5f6fb;--card:#ffffff;--ink:#151821;--muted:#5d6479;--line:#e3e6f1;
--accent:#4b50e6;--accent-ink:#ffffff;--soft:#eceeff;--ok:#1c8a5a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1017;--card:#161924;--ink:#eef0f7;
--muted:#9aa2bb;--line:#262b3d;--accent:#8e93ff;--accent-ink:#0e1017;--soft:#1d2133;--ok:#4cc38a}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:28px 24px;width:100%;max-width:440px}
.brand{font-weight:700;letter-spacing:.2px;font-size:15px;color:var(--muted);margin-bottom:18px}
.brand b{color:var(--accent)}
h1{font-size:22px;line-height:1.25;margin:0 0 8px}
p{margin:0 0 14px;color:var(--muted)}
.btn{display:block;text-align:center;text-decoration:none;font-weight:600;border-radius:14px;padding:14px 16px;margin:10px 0;
background:var(--accent);color:var(--accent-ink);border:0;width:100%;font-size:16px;cursor:pointer}
.btn.ghost{background:var(--soft);color:var(--ink)}
.btn small{display:block;font-weight:400;opacity:.8;font-size:12px;margin-top:2px}
.os{border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:10px 0}
.os.rec{border-color:var(--accent);background:var(--soft)}
.os h2{font-size:14px;margin:0 0 4px}
.tag{display:none;font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.6px}
.os.rec .tag{display:block}
.os a.btn{margin:8px 0 0;padding:11px 14px;font-size:15px}
code{display:block;word-break:break-all;background:var(--soft);border-radius:10px;padding:10px 12px;font-size:13px;color:var(--ink)}
details{margin-top:14px;color:var(--muted)}summary{cursor:pointer;font-weight:600;color:var(--ink)}
.hint{font-size:13px;color:var(--muted);margin-top:12px}
.ok{color:var(--ok);font-weight:600}
"""


def _layout(title: str, body: str, script: str = "") -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body><main class="card"><div class="brand">Ampire<b>VPN</b></div>{body}</main>
<script>{script}</script></body></html>"""


def landing_page(bot_username: str) -> str:
    body = (
        "<h1>Быстрый и стабильный VPN</h1><p>Подключение занимает минуту — всё делается в Telegram-боте.</p>"
        f'<a class="btn" href="https://t.me/{html.escape(bot_username)}">Открыть бота</a>'
    )
    return _layout("AmpireVPN", body)


def not_found_page(bot_username: str) -> str:
    body = (
        "<h1>Ссылка устарела</h1><p>Открой бота и нажми «Добавить подписку» ещё раз — мы выдадим свежую ссылку.</p>"
        f'<a class="btn" href="https://t.me/{html.escape(bot_username)}">Открыть бота</a>'
    )
    return _layout("Ссылка устарела", body)


def download_page(dead: "set[str] | frozenset[str]" = frozenset()) -> str:
    L = {k: html.escape(v, quote=True) for k, v in effective_links(dead).items()}
    body = f"""
<h1>Скачать Happ</h1>
<p id="lead">Определяем твоё устройство…</p>
<div id="list">
 <section class="os" data-os="ios"><span class="tag">Твоё устройство</span><h2>iPhone и iPad</h2>
  <a class="btn" href="{L['ios_ru']}">App Store · Россия<small>если у тебя российский Apple ID</small></a>
  <a class="btn ghost" href="{L['ios_global']}">App Store · другие страны</a></section>
 <section class="os" data-os="android"><span class="tag">Твоё устройство</span><h2>Android</h2>
  <a class="btn" href="{L['android']}">Google Play</a>
  <a class="btn ghost" href="{L['android_apk']}">Скачать APK</a></section>
 <section class="os" data-os="windows"><span class="tag">Твоё устройство</span><h2>Windows</h2>
  <a class="btn" href="{L['windows']}">Скачать для Windows<small>x64 — большинство компьютеров</small></a>
  <a class="btn ghost" href="{L['windows_arm']}">Windows ARM64</a></section>
 <section class="os" data-os="macos"><span class="tag">Твоё устройство</span><h2>macOS</h2>
  <a class="btn" href="{L['macos_ru']}">App Store · Россия</a>
  <a class="btn ghost" href="{L['macos_global']}">App Store · другие страны</a>
  <a class="btn ghost" href="{L['macos_dmg']}">Скачать .dmg</a></section>
 <section class="os" data-os="linux"><span class="tag">Твоё устройство</span><h2>Linux</h2>
  <a class="btn" href="{L['linux']}">Скачать (deb, rpm, pkg)</a></section>
</div>
<p class="hint">После установки вернись в бота и нажми «Добавить подписку».</p>
"""
    script = """
(function(){
 var ua=navigator.userAgent||"",t=navigator.maxTouchPoints||0,os="";
 if(/iPhone|iPad|iPod/i.test(ua)||(/Macintosh/i.test(ua)&&t>1))os="ios";
 else if(/Android/i.test(ua))os="android";
 else if(/Windows/i.test(ua))os="windows";
 else if(/Macintosh|Mac OS X/i.test(ua))os="macos";
 else if(/Linux|X11|CrOS/i.test(ua))os="linux";
 var list=document.getElementById("list"),lead=document.getElementById("lead");
 var names={ios:"iPhone / iPad",android:"Android",windows:"Windows",macos:"macOS",linux:"Linux"};
 if(!os){lead.textContent="Выбери своё устройство:";return;}
 var sec=list.querySelector('[data-os="'+os+'"]');
 if(sec){sec.classList.add("rec");list.insertBefore(sec,list.firstChild);}
 lead.textContent="Похоже, у тебя "+names[os]+". Нажми кнопку ниже"+((os==="android")?" — Google Play откроется сам.":".");
 if(os==="android"){var a=sec.querySelector("a.btn");setTimeout(function(){location.href=a.href;},900);}
})();
"""
    return _layout("Скачать Happ", body, script)


def add_subscription_page(sub_url: str) -> str:
    deeplink = "happ://add/" + sub_url
    body = f"""
<h1>Добавляем подписку</h1>
<p id="lead">Сейчас откроется Happ и подхватит подписку. Если ничего не происходит — нажми кнопку.</p>
<a class="btn" id="open" href="{html.escape(deeplink, quote=True)}">Открыть Happ</a>
<a class="btn ghost" href="/dl">У меня ещё нет Happ — скачать</a>
<details><summary>Добавить вручную</summary>
 <p class="hint">В Happ нажми «+» → «Вставить из буфера» (или «Добавить по ссылке») и вставь:</p>
 <code id="sub">{html.escape(sub_url)}</code>
 <button class="btn ghost" id="copy" type="button">Скопировать ссылку</button>
</details>
"""
    script = (
        "var DL=%s,SUB=%s;"
        "setTimeout(function(){location.href=DL;},450);"
        "document.getElementById('copy').onclick=function(){var b=this;"
        "function ok(){b.textContent='Скопировано ✓';}"
        "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(SUB).then(ok,fb);}else{fb();}"
        "function fb(){var r=document.createRange();r.selectNodeContents(document.getElementById('sub'));"
        "var s=getSelection();s.removeAllRanges();s.addRange(r);try{document.execCommand('copy');ok();}catch(e){}}};"
    ) % (json.dumps(deeplink).replace("</", "<\\/"), json.dumps(sub_url).replace("</", "<\\/"))
    return _layout("Добавить подписку в Happ", body, script)
