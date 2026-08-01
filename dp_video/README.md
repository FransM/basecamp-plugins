# DisplayPad Video

BaseCamp Linux plugin: plays a video file on a DisplayPad key.

This is an **action** plugin (like `dp_clock` and `dp_pipe_text`): you
assign it to a key from the DisplayPad action dropdown. The action field
holds a **pipe path**, not the video itself -- the video's filename is
delivered separately by writing it to that pipe, the same idea as the
`playvideo.py` companion script this plugin replaces. Here, decoding and
pushing frames all happens inside the plugin -- no external script, no
temporary frame files.

## Installation

```
mkdir -p ~/.config/mountain-time-sync/plugins/dp_video
cp plugin.json __init__.py ~/.config/mountain-time-sync/plugins/dp_video/
```

Requires `opencv-python` in addition to the bundled `Pillow`:

```
pip install opencv-python
```

Two prerequisites worth knowing before you install it:

* **BaseCamp 2.1.7 or newer.** The plugin asks the app which DisplayPad page
  is on screen and what is assigned to its keys. Older versions have no such
  API and the plugin stays idle.
* **A source install** (`python3 gui.py`), not the AppImage. The AppImage runs
  its own bundled Python, which cannot see packages you install with the
  system `pip`, so `opencv-python` will not be importable there.

Restart BaseCamp Linux.

## Usage

1. Open the DisplayPad action config and pick **"Video"** as the type for
   a key.
2. In the action field, enter a pipe path, e.g.:

   ```
   /tmp/dp_video.pipe
   ```

3. The plugin creates the FIFO automatically if it doesn't exist yet. Write
   the path of a video file to it to start playback on that key:

   ```bash
   echo "/home/user/clips/intro.mp4" > /tmp/dp_video.pipe
   ```

Each key with a "Video" action gets its own independent pipe and playback
thread, so multiple keys can each play something different at the same
time.

### Pipe message reference

| Message                       | Effect                                             |
| ------------------------------ | --------------------------------------------------- |
| `/path/to/video.mp4`           | Play the video once, then clear back to blank        |
| `loop /path/to/video.mp4`      | Loop the video until stopped or replaced             |
| `stop` (or an empty message)   | Stop playback and clear the key                      |

Writing a new path while a video is already playing replaces it immediately.

Pressing the assigned key **pauses/resumes** whatever is currently playing
on it (no-op if nothing is playing).

## How frames are rendered

Each frame is decoded with OpenCV, scaled down (never up) to fit within
102x102 while preserving its aspect ratio -- the same approach the original
`playvideo.py` used -- and centered on a black background. This all happens
in memory; unlike `playvideo.py`, no `.bmp` files are written to a temp
directory.

Frame timing follows the source video's own FPS (falls back to 25 fps if
the file doesn't report one), the same as `playvideo.py`.

## How it works internally

Like `dp_clock` and `dp_pipe_text`, this plugin registers an action type
(`dp_video`) and runs a background scan loop (every ~2s) over the
DisplayPad action config. For every key assigned the "Video" type, it
starts a dedicated pipe-reader thread bound to the path in that key's
action field; if you change the path, the old reader (and any playback
using it) is stopped and a new one started automatically. If the action is
removed from a key, its reader and playback are stopped and the old FIFO
is deleted from disk (only if it's still a FIFO).

Each reader opens its pipe non-blocking and uses `select()` so it can be
shut down cleanly; a full message is whatever gets written between one pipe
open and close (matching typical `echo ... > pipe` usage). Video frames are
pushed directly without the disk-cache trick `dp_clock`/`dp_pipe_text` use
for static content -- doing that on every frame would add needless disk
I/O at video framerates. A blank/cleared key *is* persisted that way, so it
survives a page switch/reload.

## Limitations / notes

- Key assignments are re-scanned every ~2 seconds, so a newly assigned key
  or a changed pipe path takes a moment to take effect.
- Writing to the pipe (`echo ... > pipe`) blocks briefly until the plugin
  has the pipe open for reading -- this happens almost immediately, but if
  you want to guarantee your script never blocks, write with
  `printf ... > pipe &`.
- Only one video per key at a time; sending a new path replaces the
  currently playing one.
- Audio is not played -- the DisplayPad has no speaker, only frames are
  rendered.
