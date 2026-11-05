# Theater walkway lights

A depth camera watches the left and right walkways. When someone gets up and walks out, the lights on that side come on very low. People sitting in their seats don't trigger anything. Someone walking back to their seat can be ignored, and the lights fade sooner once they sit down.

Privacy: only the camera's two infrared depth sensors are used. The color camera is never turned on, and no images or video are saved. Each depth frame becomes a yes or no per walkway and is then thrown away. Everything runs on your own network, with nothing sent to the cloud.

## What to buy

- **Luxonis OAK-D Pro W PoE** depth camera. "Pro" adds an infrared illuminator, so it works in the dark. "W" is the wide-angle model, so one camera can see both walkways. The PoE version runs over a single Ethernet cable, so the camera can be in the theater while the code runs on a server in the rack.
- **A PoE switch port or a PoE injector** for the camera.
- **A computer to run the code.** Any Windows or Linux machine on the same network works, including the rack server. You need Python 3.10 or newer.
- **An MQTT broker in Home Assistant** (the Mosquitto add-on) if you don't already run one.

## Mounting

Either position works, and auto-level works out the angle for you.

- **Ceiling, over the seats (most reliable).** Center it between the walkways, pointing straight down. Turn it so the long side of the image runs from walkway to walkway, with the top of the image toward the screen.
- **Back wall, high up.** Center it and tilt it down toward the screen, roughly 40–60°. Keep the top of the image toward the screen and level it side to side by eye. Auto-level corrects any small lean. A back-wall camera sees the whole length of both walkways. People far away cover fewer pixels, so detection scores are corrected for distance, and someone near the screen counts about the same as someone near the camera. The one weak spot is that a person standing in the back row can briefly hide the walkway in front of them. Mount it as high as you can to keep that short.

## Install

### Windows (rack server)

From an **Administrator** PowerShell in the repo folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

The first run creates `config.yaml` from the example and stops so you can enter your MQTT login. Run it again after that. The script:

- installs Python packages,
- registers a **"Theater Walkway"** task that starts with Windows and restarts within a minute if it stops,
- opens the firewall for the camera and the setup page,
- starts the tracker with the setup page on port 8099.

Logs go to `logs\walkway.log`. Re-run the script to update, or run `scripts\uninstall-windows.ps1` to remove it. Add `-NoSetupPage` if you don't want the page running all the time.

### Linux

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml          # enter your MQTT login
python -m walkway config.yaml --setup       # or set it up as a systemd service
```

The PoE camera is found on the network automatically. If it isn't, check that the server and camera are on the same subnet or VLAN.

## Setup

1. **Auto-level.** Open `http://<server>:8099`, clear the floor of anything big, and press **Auto-level camera**. The tracker finds the floor and saves the camera's height, tilt and side-to-side roll, so you don't have to measure anything. To do this without the page, run `python -m walkway config.yaml --calibrate`. If the camera is on the back wall, set `camera.y` in `config.yaml` to a negative number, such as `-4.5`, so the map puts it behind the seats. Its exact value doesn't matter.
2. **Draw zones.** The page shows a live view from above, where brighter means taller, so seats and people show up and faces never do. Drag on it to draw a zone for each walkway, then drag a zone to move it or drag its corner to resize it.
3. **Pick the way out.** For each zone, choose **Turn on when someone walks**: *toward the back (leaving)*, *toward the screen (leaving)*, or *either way*. With a direction set, the zone only turns on for someone heading out. If they walk back toward the seats, the lights fade after 5 seconds instead of 30. An arrow on the map shows which way is out.
4. **Check it.** Walk each walkway, and sit in the seats next to it. Each zone shows a live score and turns green when it's on. If sitting triggers it, raise "From height" or "Points needed". Press **Save to config** when it's right.
5. **Home Assistant.** Each zone appears automatically as a sensor, for example `binary_sensor.theater_walkway_left_walkway`. Add the automations in `homeassistant/walkway_lights.yaml`, using your own light entities.

## Movie-mode switch

Home Assistant also gets a switch, `switch.theater_walkway_detection`. Turn it off during cleaning, or whenever the room lights are on, and the tracker stops turning zones on. The setup page shows whether it's on, and the tracker remembers the setting across restarts. To flip it automatically, add an automation that turns it on when your projector or media player starts and off when it stops.

## Trying it without the camera

```bash
python -m walkway config.example.yaml --sim --setup  # setup page with a fake person in the left walkway
python -m walkway config.example.yaml --sim --tune   # same, printing zone scores
python -m pytest
```

## Tuning

| Setting | Default | Raise it if | Lower it if |
| --- | --- | --- | --- |
| `z_min` (From height) | 1.0 m | seat backs or people leaning trigger it | short people or kids are missed |
| `min_points` (Points needed) | 100 | it triggers on nothing | people walking through are missed |
| `on_frames` | 3 | it flickers on briefly | it reacts too slowly |
| `off_seconds` | 30 | lights go off before they're out of the room | lights stay on too long |
| `return_off_seconds` | 5 | lights go off before they've sat down | lights linger after they're back |

The camera code (`walkway/sources.py`, `OakDepthSource`) uses the depthai 2.x API and hasn't been tested on real hardware yet. The geometry, auto-level, direction logic, Home Assistant switch and setup page are covered by tests that use a simulated camera, with ceiling and back-wall mounts. The Windows install scripts haven't been run on Windows yet.
