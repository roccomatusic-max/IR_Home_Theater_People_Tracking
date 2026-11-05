# Theater walkway lights

An overhead depth camera watches the left and right walkways. When someone stands up and walks into one, that side's walkway lights come on very low. People sitting in their seats don't trigger anything.

Privacy: only the camera's two infrared depth sensors are used. The color camera is never turned on, and no images or video are saved. Each depth frame is turned into a yes/no per walkway and then thrown away. All of this runs on a computer in the room, with nothing sent to the cloud.

## What to buy

- **Luxonis OAK-D Pro W PoE** depth camera. "Pro" adds an infrared illuminator, so it works in the dark. "W" is the wide-angle model, so one camera can see both walkways. The PoE version runs over a single Ethernet cable, so the camera can sit on the theater ceiling while the code runs on a server in the rack.
- **A PoE switch port or a PoE injector** for the camera.
- **A computer to run the code.** Any Windows or Linux machine on the same network works, including the rack server. Python 3.10 or newer.
- An MQTT broker in Home Assistant (the Mosquitto add-on) if you don't already run one.

## Mounting

Mount it on the ceiling, centered between the two walkways and roughly over the seats, pointing straight down. Turn it so the long side of the image runs across the room, from walkway to walkway, with the top of the image facing the screen. The camera has to see each walkway from about 1 m off the floor up to head height. Check this with `--heightmap` (below) before you fine-tune the zones. If a walkway is too far out to the side, tilt the camera or move it closer to that wall.

## Setup

Windows (PowerShell):

```powershell
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.yaml config.yaml   # then edit: camera height, walkway boxes, MQTT login
```

Linux:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

The PoE camera is found on the network automatically. If it isn't, make sure the server and camera are on the same subnet or VLAN, and that Windows Firewall allows Python on that network.

1. Measure each walkway from the spot on the floor directly under the camera. Put those measurements into `zones` in `config.yaml`. Left of center is negative x, toward the screen is positive y.
2. Run `python -m walkway config.yaml --heightmap map.png` and open `map.png`. It shows how tall things are from above, with your zone boxes drawn on it. Brighter means taller, so seat backs show up as bright bands. Adjust the boxes until they cover only the walkways.
3. Run `python -m walkway config.yaml --tune` and walk each walkway. Each zone's point count should jump well above `min_points` while you're in it and stay near 0 while you're seated.
4. Run `python -m walkway config.yaml`. Two sensors appear in Home Assistant, `binary_sensor.theater_walkway_left_walkway` and `binary_sensor.theater_walkway_right_walkway`.
5. Add the automations in `homeassistant/walkway_lights.yaml`, using your own light entities.

To run it at boot, run `python -m walkway C:\\path\\to\\config.yaml` as a Windows service (for example with NSSM) or a Task Scheduler task "at startup". On Linux, use a systemd service.

## Trying it without the camera

```bash
python -m walkway config.example.yaml --sim --tune   # a fake person stands in the left walkway
python -m pytest
```

## Tuning

| Setting | Default | Raise it if | Lower it if |
| --- | --- | --- | --- |
| `z_min` | 1.0 m | seat backs or people leaning trigger it | short people or kids are missed |
| `min_points` | 150 | it triggers on nothing | people walking through are missed |
| `on_frames` | 3 | it flickers on briefly | it reacts too slowly |
| `off_seconds` | 30 | lights go off before they're out of the room | lights stay on too long |

The camera code (`walkway/sources.py`, `OakDepthSource`) uses the depthai 2.x API and hasn't been tested on real hardware yet. The geometry and zone logic are covered by the tests, which use a simulated camera.
