# raccoon.service braucht 10–20 s zum Stoppen

Status: Analyse (nur Code gelesen, noch nichts geändert, nicht auf dem Pi verifiziert)

## Problem

Beim Shutdown/Restart braucht `raccoon.service` teilweise 10–20 s, bis es
gestoppt ist. Die Dauer schwankt von Mal zu Mal.

## Ursache

Der Long-Poll-Endpoint für den Netzwerkstatus hält den Server beim Stoppen auf
(`raccoon_cli/server/routes/network.py:267-275`):

```python
timeout: float = Query(default=20.0, ge=0.0, le=30.0)
return await network_events.wait_async(since, timeout)
```

- BotUI und CLI haben dauerhaft einen solchen Request offen. Er blockiert bis
  zu 20 s (maximal 30 s), solange sich Kabel oder Netzwerkmodus nicht ändern.
- Bei `SIGTERM` macht uvicorn einen graceful shutdown: Er schließt den Listener
  und wartet, bis alle laufenden HTTP-Requests fertig sind.
- Die Long-Poll-Request weiß nichts vom Shutdown. Die Restzeit hängt davon ab,
  wann der letzte Poll gestartet wurde (ca. 0–20 s). Das erklärt die
  schwankende Dauer.
- Die Wartezeit liegt immer unter dem systemd-Default `TimeoutStopSec=90s`.
  Es gibt deshalb kein `SIGKILL`, nur ein langsames Stoppen.

Verstärkende Faktoren:

- `raccoon_cli/server/__main__.py`: `uvicorn.run(...)` ohne
  `timeout_graceful_shutdown`, also keine Obergrenze.
- `raccoon_cli/systemd/raccoon.service`: kein `TimeoutStopSec`, Default 90 s.
- `lifespan` in `raccoon_cli/server/app.py` setzt nur `watchdog_stop`. Es weckt
  keine wartenden Long-Poller und bricht keine laufenden Commands ab.

Nicht die Ursache:

- `ip monitor` wird per `terminate()` beendet, die Watcher-Threads sind Daemons.
- Command-Executor: 2 s Terminate-Timeout, Prozesse in eigener Session und in
  der cgroup, bekommen ohnehin `SIGTERM` von systemd.
- WebSockets (`/ws/output`) schließt uvicorn selbst.

## Vorgeschlagener Fix

1. **Hauptfix:** Im `lifespan` nach `yield` die Long-Poll-Waiter sofort
   aufwecken, z. B. `network_events.shutdown()`. Dabei werden alle Futures in
   `_waiters` gesetzt, und neue Polls antworten ab dann sofort.
2. **Absicherung:** `uvicorn.run(..., timeout_graceful_shutdown=3)` in
   `raccoon_cli/server/__main__.py`.
3. **Optional:** `TimeoutStopSec=10` in `raccoon_cli/systemd/raccoon.service`
   und in der Inline-Kopie der Unit in `raccoon_cli/server/cli.py`
   (`_do_install`).

## Verifikation auf dem Pi

Vorher bestätigen:

```bash
journalctl -u raccoon -b -1 | grep -E "Waiting for connections|Shutting down|Stopped"
```

"Waiting for connections to close" sollte etwa so lange vor "Stopped" stehen,
wie das Stoppen dauert.

Nach dem Fix:

```bash
time sudo systemctl stop raccoon
```

Mit offener BotUI sollte das Stoppen nach unter 3 s fertig sein.
