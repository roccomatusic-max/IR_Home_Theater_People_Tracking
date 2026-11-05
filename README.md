# Theater walkway lights

A depth camera tracks everyone in the theater. When someone stands up, the walkway next to them is **pre-armed**, so its lights come on, very low, the instant they step into it. People sitting in their seats don't trigger anything. Someone walking back to their seat can be ignored, and the lights fade sooner once they sit down. Home Assistant also gets a head count and a "theater occupied" sensor, with an optional auto-off when the room is empty.

Privacy: only the camera's two infrared depth sensors are used. The color camera is never turned on, and no images or video are saved. Each depth frame becomes positions and heights of anonymous blobs, then a yes or no per walkway, and is then thrown away. A tracked person is a number that's forgotten when they leave, never an identity. The only thing saved is the empty room's height map, a coarse 10 cm grid of numbers. Everything runs on your own network, with nothing sent to the cloud.

## What to buy

- **Luxonis OAK-D Pro W PoE** depth camera. "Pro" adds an infrared illuminator, so it works in the dark. "W" is the wide-angle model, so one camera can see both walkways. The PoE version runs over a single Ethernet cable, so the camera can be in the theater while the code runs on a server in the rack.
- **A PoE switch port or a PoE injector** for the camera.
- **A computer to run the code.** Any Windows or Linux machine on the same network works, including the rack server. You need Python 3.10 or newer.
- **An MQTT broker in Home Assistant** (the Mosquitto add-on) if you don't already run one.

## Mounting

Either position works, and auto-level works out the angle for you.

- **Ceiling, over the seats.** Center it between the walkways, pointing straight down. Turn it so the long side of the image runs from walkway to walkway, with the top of the image toward the screen. Straight down, it only sees heads within about 1.2 m along the room and 1.8 m across from a 2.7 m ceiling, so it suits small rooms. Tilting it toward the screen helps.
- **Back wall, high up.** Center it and tilt it down toward the screen, roughly 40–60°. Keep the top of the image toward the screen and level it side to side by eye. Auto-level corrects any small lean. A back-wall camera sees the whole length of both walkways. People far away cover fewer pixels, so detection scores are corrected for distance, and someone near the screen counts about the same as someone near the camera. The one weak spot is that a person standing in the back row can briefly hide the walkway in front of them. Tracking bridges gaps of up to a second, and mounting it as high as you can keeps them short. **For whole-room tracking, this is the better position.**

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
2. **Learn the empty room.** With everyone out of the room, press **Learn empty room**. It takes a few seconds. The tracker remembers how tall the seats are, so it can then tell a seated person apart from an empty seat. Redo this after auto-leveling or moving furniture. If the camera moves, the tracker notices and forgets the old map. Without it, walkways still work, but only standing people are tracked and there's no head count.
3. **Draw zones.** The page shows a live view from above, where brighter means taller, so seats and people show up and faces never do. Drag on it to draw a zone for each walkway, then drag a zone to move it or drag its corner to resize it.
4. **Pick the way out.** For each zone, choose **Turn on when someone walks**: *toward the back (leaving)*, *toward the screen (leaving)*, or *either way*. With a direction set, the zone only turns on for someone heading out. If they walk back toward the seats, the lights fade after 5 seconds instead of 30. An arrow on the map shows which way is out.
5. **Check it.** Walk each walkway, and sit in the seats next to it. Each zone shows a live score and turns green when it's on. If sitting triggers it, raise "From height" or "Points needed". Press **Save to config** when it's right.
6. **Home Assistant.** Each zone appears automatically as a sensor, for example `binary_sensor.theater_walkway_left_walkway`. Add the automations in `homeassistant/walkway_lights.yaml`, using your own light entities.

## How it decides

1. **Find people.** Anything taller than the empty room by 25 cm or more, and at least 0.7 m off the floor, is a person. If their head is above 1.45 m they're standing, otherwise they're seated.
2. **Follow them.** Each person is followed from frame to frame. Their position is smoothed, their walking speed and direction are estimated, and they're held for up to a second if someone or something blocks the view.
3. **Pre-arm.** When someone is standing in the seats within 1.2 m of a walkway, that walkway is armed for 8 seconds. An armed walkway turns on the first frame they step into it, about a fifteenth of a second, instead of waiting for three frames and a direction check.
4. **Walkway on or off.** A walkway turns on when it's armed, or when it has seen someone for three frames and they're heading out. It turns off after 30 seconds clear, or 5 seconds if they walked back to their seat.

On the camera itself, high-accuracy stereo, subpixel depth (sharper at the far end of the room) and speckle, temporal, spatial and median filters clean up the depth before it's used.

## Room-empty auto-off

`binary_sensor.theater_walkway_room` (**Theater occupied**) turns off after `room.empty_minutes` (default 5) with nobody seen, and `sensor.theater_walkway_people` gives the head count. Both need the empty-room map.

The **Auto-off when room is empty** switch (`switch.theater_walkway_empty_auto_off`) starts **off**. The example automation in `homeassistant/walkway_lights.yaml` only turns things off while it's on, so you decide when, for example by turning it on at night only.

## Keeping it running

- **Camera reconnects.** If the camera drops (cable, PoE switch, power blip) or goes quiet for 5 seconds, the tracker reconnects on its own, waiting 1 s at first and backing off to 30 s between tries. The walkway sensors stay where they were in the meantime.
- **Health in Home Assistant.** You get `binary_sensor.theater_walkway_camera` (online), `sensor.theater_walkway_fps`, `sensor.theater_walkway_delay` (processing time per frame, in ms) and `binary_sensor.theater_walkway_camera_moved`. Alert on the camera going offline or on "camera moved" with an ordinary automation.
- **Bump detection.** Once a minute the tracker re-finds the floor. If three checks in a row disagree with the saved height or tilt (more than 6 cm or 2.5°), it flags **Camera moved** and tells you to auto-level again. A single odd reading, like someone lying on the floor, is ignored.
- **What happened.** The setup page lists recent events, newest first, and every event is also written to `events.jsonl` next to `config.yaml`, rotated at 1 MB. Events are walkway on/off, pre-arm, room occupied or empty, camera offline or moved, and switch changes. They're names and numbers, never images.

## Walk test

Press **Start walk test** on the setup page. Then walk each walkway a few times, and sit down and stand up in the seats next to them. Press **Stop and show report**. For each zone it shows:

- how strong a weak but real walk-through scored,
- the highest score while nobody was walking in it (seated neighbours, blankets, noise),
- how many times it fired,
- a suggested **Points needed** with an **Apply** button.

If walking and sitting look too similar, it tells you to raise the zone's height limit, pull the zone away from the seats, or move the camera instead.

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
| `room.arm_distance` | 1.2 m | people standing at the far seat aren't pre-arming | standing near the walkway arms it when they're not leaving |
| `room.arm_seconds` | 8 | slow walkers lose the pre-arm | it stays armed too long after someone sits back down |
| `room.empty_minutes` | 5 | "occupied" turns off during a quiet scene | auto-off takes too long |
| `oak.resolution` | 400p | far seats are missed (back-wall mount) | the computer can't keep up |

The camera code (`walkway/sources.py`, `OakDepthSource`) uses the depthai 2.x API and hasn't been tested on real hardware yet. The geometry, auto-level, people finding, tracking, pre-arming, direction logic, Home Assistant sensors and switches, setup page, reconnects, bump detection, the event log and the walk test are covered by 63 tests. They use a simulated room with two rows of seats, ceiling and back-wall mounts, 1% depth noise and 10% missing pixels. The Windows install scripts haven't been run on Windows yet.
