#!/usr/bin/env python3
"""
szyfruj.py — szyfruje gotowy dashboard (AES-256-GCM, klucz z hasła przez PBKDF2-SHA256).

Wynik to strona, która odszyfrowuje się w przeglądarce:
  • link z kluczem:  https://…/#k=HASLO   → otwiera się od razu,
  • bez klucza       → pole do wpisania hasła (można zapamiętać na urządzeniu).
Hasło nigdy nie trafia na serwer (część adresu po „#” nie jest wysyłana).

Użycie:  STRONA_KLUCZ=… python szyfruj.py strona/index.html
Bez STRONA_KLUCZ plik zostaje zastąpiony informacją, że brakuje klucza (nic nie wycieka).
"""
import base64
import os
import secrets
import sys
from pathlib import Path

ITER = 250_000

SZABLON = """<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Stacja pogodowa</title>
<style>
body{margin:0;background:#0b0b0b;color:#ecebe8;font:15px/1.5 "Segoe UI",system-ui,-apple-system,Helvetica,Arial,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
.box{background:#1d1d1d;border:1px solid #2f2f2f;border-radius:12px;padding:28px 26px;width:min(360px,90vw)}
h1{font-size:19px;margin:0 0 6px}p{color:#9b978f;font-size:13px;margin:0 0 16px}
input[type=password]{width:100%;box-sizing:border-box;background:#242424;border:1px solid #3a3a3a;border-radius:8px;color:#ecebe8;padding:10px 12px;font-size:15px}
button{margin-top:12px;width:100%;background:#f3f2ef;color:#111;border:0;border-radius:8px;padding:10px;font-size:15px;cursor:pointer}
label{display:flex;gap:8px;align-items:center;color:#9b978f;font-size:13px;margin-top:12px}
.err{color:#e07a5f;font-size:13px;min-height:18px;margin-top:8px}
</style></head><body>
<form class="box" id="f" style="display:none"><h1>Stacja pogodowa</h1><p>Strona jest zabezpieczona. Wpisz hasło albo otwórz link z kluczem.</p>
<input type="password" id="h" autocomplete="current-password" placeholder="Hasło" autofocus>
<label><input type="checkbox" id="z" checked> Zapamiętaj na tym urządzeniu</label>
<button type="submit">Otwórz</button><div class="err" id="e"></div></form>
<script>
const S="__SALT__", I="__IV__", C="__CT__", N=__ITER__;
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function odszyfruj(haslo){
  const k0=await crypto.subtle.importKey("raw",new TextEncoder().encode(haslo),"PBKDF2",false,["deriveKey"]);
  const k=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b64(S),iterations:N,hash:"SHA-256"},k0,{name:"AES-GCM",length:256},false,["decrypt"]);
  const t=await crypto.subtle.decrypt({name:"AES-GCM",iv:b64(I)},k,b64(C));
  return new TextDecoder().decode(t);
}
function pokaz(html){ document.open(); document.write(html); document.close(); }
function zapamietane(){ try{ return localStorage.getItem("stacja_klucz"); }catch(e){ return null; } }
function zapamietaj(h){ try{ localStorage.setItem("stacja_klucz",h); }catch(e){} }
function zapomnij(){ try{ localStorage.removeItem("stacja_klucz"); }catch(e){} }
(async()=>{
  const m=location.hash.match(/[#&]k=([^&]+)/);
  const kand=[m?decodeURIComponent(m[1]):null, zapamietane()].filter(Boolean);
  for(const h of kand){
    try{ const html=await odszyfruj(h); if(m) zapamietaj(h); pokaz(html); return; }catch(e){ if(h===zapamietane()) zapomnij(); }
  }
  const f=document.getElementById("f"); f.style.display="";
  if(m) document.getElementById("e").textContent="Klucz w linku jest nieaktualny.";
  f.onsubmit=async ev=>{ ev.preventDefault(); const h=document.getElementById("h").value;
    try{ const html=await odszyfruj(h); if(document.getElementById("z").checked) zapamietaj(h); pokaz(html); }
    catch(e){ document.getElementById("e").textContent="Nieprawidłowe hasło."; } };
})();
</script></body></html>"""

BRAK = """<!DOCTYPE html><html lang="pl"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Stacja pogodowa</title></head><body style="background:#0b0b0b;color:#ccc;font:15px sans-serif;padding:40px">
Strona jest zabezpieczona, ale nie ustawiono klucza (sekret STRONA_KLUCZ).</body></html>"""


def main() -> None:
    p = Path(sys.argv[1] if len(sys.argv) > 1 else "strona/index.html")
    haslo = os.getenv("STRONA_KLUCZ", "")
    if not haslo:
        p.write_text(BRAK, encoding="utf-8")
        print("::error::Brak sekretu STRONA_KLUCZ – opublikowano tylko informację, bez danych")
        return
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    sol, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    klucz = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=sol, iterations=ITER).derive(haslo.encode())
    szyfr = AESGCM(klucz).encrypt(iv, p.read_bytes(), None)
    e = lambda b: base64.b64encode(b).decode()
    html = (SZABLON.replace("__SALT__", e(sol)).replace("__IV__", e(iv))
            .replace("__ITER__", str(ITER)).replace("__CT__", e(szyfr)))
    p.write_text(html, encoding="utf-8")
    print(f"Zaszyfrowano {p} ({len(html) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
