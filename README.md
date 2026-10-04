# Proxis CCTV — Home Assistant integrations

Home Assistant custom integrations for CCTV hardware that has no official
support.

| Integration | Device family | Status |
|---|---|---|
| [PX IPC](custom_components/px_ipc) | HeroSpeed / Longse IP cameras (`KL8`, `NL4`, … platform) | working, verified on hardware |

Icons and logos live in `custom_components/<domain>/brand/`, which is where Home
Assistant serves custom-integration branding from. Both a light and a dark
variant are shipped: the mark is orange and near-black, and its black half would
disappear against Home Assistant's dark theme, so the dark variant swaps that
near-black for white.

---

## PX IPC

Local integration for PX IPC cameras. No cloud, no vendor account — it talks to
the camera's own JSON API on your LAN.

### Verified against

```
Device      PX_IPC  (manufacturer: HeroSpeed)
Firmware    KL8_1ND_BVD5L1A0T1Q0_K300036141_V2.0.12.260306_R1
Streams     3840x2160 main, 720x480 sub, H.264, RTSP
```

The protocol is shared across the Longse/HeroSpeed IPC line, so other models on
the same platform should work even though only this one has been tested. The
camera has **no ONVIF**, so Home Assistant's ONVIF integration cannot be used.

### What you get

| Platform | Entities |
|---|---|
| `camera` | Live RTSP stream (main or sub) + still image from the API |
| `binary_sensor` | 17 event sensors: motion, region intrusion, illegal parking, line crossing, region entry/exit, loitering, people gathering, face detection, video tampering, audio detection, **license plate**, scene change, abandoned object, object removed, alarm input, video loss |
| `sensor` | **Last license plate**, last event, event-stream state, firmware, model, serial, platform |
| `select` | Day/night mode, illuminator (warm light / infrared / intelligent), WDR level |
| `number` | Illuminator brightness |

Event sensors are driven by the camera's **WebSocket event stream**, not by
polling: the camera pushes a packet the moment it detects something. Each sensor
latches for 30 seconds, and an explicit "cleared" packet switches it off
immediately.

### Installation

**HACS** — add `https://github.com/pirsasha/proxiscctv` as a custom repository
(category: Integration), install, restart Home Assistant.

**Manually** — copy `custom_components/px_ipc` into your Home Assistant
`config/custom_components/` directory and restart.

Then: *Settings → Devices & Services → Add integration → PX IPC Camera*.

| Field | Notes |
|---|---|
| Host | camera address, e.g. `192.168.2.223` |
| Port | `80` by default |
| Username / Password | the camera's web account, `admin` by default |
| Stream | `main` (full resolution) or `sub` (low bandwidth) |

The stream can be changed later in the integration's options without removing
the device.

### There is no automatic discovery — you must type the address

This was tested against the hardware rather than assumed:

| Mechanism | Result |
|---|---|
| SSDP / UPnP `M-SEARCH` (`ssdp:all`, `Basic:1`, `NetworkVideoTransmitter:1`, `MediaServer:1`, `upnp:rootdevice`) | no reply to any of them |
| mDNS (`_http._tcp`, `_rtsp._tcp`, `_onvif._tcp`, `_services._dns-sd._udp`) | no reply to any of them |
| ONVIF | every `/onvif/*` path answers 404, so Home Assistant's ONVIF discovery cannot see it |
| DHCP | the camera ships with `enableDhcp: false` — it holds a static address, so it never makes a DHCP request Home Assistant could match on |

The camera simply announces nothing. So a manual host entry is the supported
path, and asking the user to type an IP is not laziness — there is nothing to
discover from.

To find the address the first time: check the DHCP/client list on your router, or
use the vendor's search tool, or the `P2P` serial number the camera reports
(`/api/network/p2p`) with the vendor app.

**Tip:** give the camera a static lease in your router rather than changing its
own network settings, otherwise a re-addressed camera means editing the
integration.

### Example automation

Open a gate when your own car arrives:

```yaml
automation:
  - alias: "Open the gate for our car"
    triggers:
      - trigger: state
        entity_id: sensor.dvor_last_license_plate
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.state == 'А123ВС777' }}"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.gate
```

### Read this before expecting plates

**The camera rarely reports a plate, and this is a hardware/firmware limit, not
an integration bug.**

The camera has its own plate recogniser and an on-board plate library, and its
SDK documents a WebSocket packet (`packet_type == 6`) that carries
`license_plate_list[].license_plate_num`. But:

* its HTTP alarm notification carries **no** plate text at all — verified on the
  device and confirmed by the vendor SDK, whose object list only allows
  `human` / `vehicle` / `face` / `item`;
* the plate packet only appears once the camera's **own** recogniser reads a
  plate, which needs a large, sharp plate in frame. On a wide-angle camera
  covering a whole yard, plates sit at the very edge of the minimum size
  (64x16 px), so in practice it often never fires;
* the firmware in the field has no plate-history API either —
  `/api/event/stat-plate-recognition` and `/api/event/plate-lib` do not exist on
  `V2.0.12.260306` and answer by dropping the connection.

So `sensor.*_last_license_plate` shows what the **camera** actually recognised
and stays `unknown` otherwise. It never invents a value. If you need a plate for
every car, run your own recognition on the RTSP stream and feed Home Assistant
from there.

### Debugging

```yaml
logger:
  logs:
    custom_components.px_ipc: debug
```

Useful things to know:

* the camera sends **nothing** on the event stream while the scene is quiet — it
  does not send the heartbeats its own SDK documents, so silence is normal;
* every camera reboot invalidates the session; the integration logs in again by
  itself, so you do not have to reload it;
* writes to the camera **require** `Content-Type: application/json; charset=utf-8`.
  Without the charset the device answers `code: 0` and silently ignores the
  change, which looks exactly like "the integration does not save";
* WDR lives only on the legacy `/api/image/image` endpoint
  (`enableWideDynamic`). The modern `/api/image/image-param` accepts
  `wideDynamicLevel`, answers `code: 0` and changes nothing;
* `code: 101` means the session expired, `code: 203` means "endpoint known but
  not supported by this firmware".

### Tests

```bash
python -m pytest tests -q
```

14 tests cover the login hash chain and the packet parser. The login digest is
pinned against a known-good vector produced by an independent implementation and
confirmed to be accepted by real hardware, so a change that breaks authentication
cannot pass by agreeing with itself.

---

## License

MIT — see [LICENSE](LICENSE).
