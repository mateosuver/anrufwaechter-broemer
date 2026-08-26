"""
Einmaliger interaktiver Login (Device Code Flow) für den Graph-Mailversand.

Läuft NICHT als Teil der Middleware selbst -- einmal manuell ausführen. Zeigt
eine URL + einen Code an, die im Browser eingegeben werden; danach wird ein
MSAL-Token-Cache (inkl. Refresh Token) unter settings.graph_token_cache_path
gespeichert. email_client.py liest diesen Cache beim Mailversand und erneuert
den Access Token automatisch daraus (acquire_token_silent), solange der
Refresh Token noch gültig ist -- ohne dass dieses Skript erneut laufen muss.

WICHTIG (Delegated-Flow, kein Client Secret -- gewählt, weil dieser Account
keine Admin-Rechte im Tenant hat, s. config.py): der Refresh Token gehört dem
angemeldeten Benutzer, nicht der App, und stirbt:
  - nach 90 Tagen Inaktivität
  - bei jedem Passwortwechsel
  - bei Session-Revocation durch eine Conditional-Access-Policy
Die Middleware hört dann OHNE Fehlermeldung auf, Mails zu schicken (loggt
nur eine Warnung) -- dann dieses Skript hier erneut manuell ausführen.

NUR NÖTIG, wenn GRAPH_CLIENT_SECRET in .env LEER ist (s. config.py). Ist ein
Admin verfügbar (Client-Credentials-Variante, GRAPH_CLIENT_SECRET gesetzt),
läuft der Mailversand ohne dieses Skript und ohne die obige Einschränkung --
volle Anleitung dafür in graphsendmailsetup.md.

Voraussetzung in Entra ID (einmalig, s. Chatverlauf/graphsendmailsetup.md):
  - App-Registrierung mit "Allow public client flows" = Yes
  - API permissions -> Microsoft Graph -> Delegated -> Mail.Send
  - GRAPH_CLIENT_ID + GRAPH_TENANT_ID in .env eingetragen

Ausführen:
    .venv/Scripts/python.exe graph_device_login.py
"""
import sys

import msal

from config import settings

# "offline_access" NICHT hier auflisten -- MSAL fügt es (zusammen mit "openid",
# "profile") bei jedem interaktiven Flow automatisch hinzu und wirft einen
# ValueError, wenn man es selbst mit angibt ("reserved scope").
SCOPES = ["Mail.Send"]


def main() -> None:
    if not settings.graph_client_id or not settings.graph_tenant_id:
        print("GRAPH_CLIENT_ID / GRAPH_TENANT_ID fehlen in .env -- bitte erst eintragen.")
        sys.exit(1)

    cache = _load_cache()
    app = msal.PublicClientApplication(
        settings.graph_client_id,
        authority=f"https://login.microsoftonline.com/{settings.graph_tenant_id}",
        token_cache=cache,
    )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device-Flow konnte nicht gestartet werden: {flow}")

    print(flow["message"])  # "Gehe zu https://microsoft.com/devicelogin und gib den Code XXXXXXXX ein"

    result = app.acquire_token_by_device_flow(flow)  # blockiert, bis im Browser bestätigt (oder Timeout)
    if "access_token" not in result:
        raise RuntimeError(
            f"Login fehlgeschlagen: {result.get('error')} / {result.get('error_description')}"
        )

    _save_cache(cache)
    print(f"Erfolgreich angemeldet als {result.get('id_token_claims', {}).get('preferred_username')}.")
    print(f"Token-Cache gespeichert unter: {settings.graph_token_cache_path}")


def _load_cache() -> "msal.SerializableTokenCache":
    cache = msal.SerializableTokenCache()
    try:
        with open(settings.graph_token_cache_path, "r", encoding="utf-8") as f:
            cache.deserialize(f.read())
    except FileNotFoundError:
        pass
    return cache


def _save_cache(cache: "msal.SerializableTokenCache") -> None:
    if cache.has_state_changed:
        with open(settings.graph_token_cache_path, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


if __name__ == "__main__":
    main()
