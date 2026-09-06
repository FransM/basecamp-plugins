"""DisplayPad Video -- play a video file on a DisplayPad key.

This is an *action* plugin, following the same pattern as dp_clock and
dp_pipe_text: register an action type, then let the user assign it to any
key (K1-K12) via the DisplayPad action config. The action value the user
types in is the path to a named pipe (FIFO) -- not the video itself. The
video *filename* is delivered separately, by writing it to that pipe, just
like the original playvideo.py companion script did (it wrote
"<key> <frame path>" lines to a pipe). Here the whole pipeline -- decoding
frames and pushing them to the key -- runs inside the plugin itself.

Setup
-----
1. In the DisplayPad action config, pick "Video" as the type for a key.
2. Enter a pipe path in the action field, e.g.:

       /tmp/dp_video.pipe

3. The plugin creates the FIFO automatically if it doesn't exist yet. Write
   the path of a video file to it to start playback on that key:

       echo "/home/user/clips/intro.mp4" > /tmp/dp_video.pipe

   Prefix with "loop " to loop the video until stopped or replaced:

       echo "loop /home/user/clips/idle.mp4" > /tmp/dp_video.pipe

   Write "stop" (or an empty message) to stop playback and clear the key:

       echo "stop" > /tmp/dp_video.pipe

If you later change the pipe path in a key's action field, or remove the
"Video" action from a key, the plugin stops any playback for that key and
deletes the old FIFO from disk (only if it's still a FIFO), so stale pipes
don't accumulate.

Pressing the assigned key pauses/resumes the video currently playing on it.

Each frame is decoded with OpenCV, scaled down to fit within the 102x102
key (preserving aspect ratio, same approach as the original
playvideo.py), and centered on a black background -- all in memory, with
no temporary frame files written to disk.

Requires the `opencv-python` package (`pip install opencv-python`).
"""

import hashlib
import os
import select
import stat
import threading
import time

from PIL import Image

try:
    import cv2
except ImportError:
    cv2 = None

_SIZE = 102
_BG = (0, 0, 0)


def _frame_to_image(frame):
    """Convert a BGR OpenCV frame to a 102x102 RGB PIL image, letterboxed
    on a black background while preserving aspect ratio."""
    h, w = frame.shape[:2]
    if max(w, h) > _SIZE:
        scale = _SIZE / max(w, h)
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        new_h, new_w = h, w

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    frame_img = Image.fromarray(rgb)

    canvas = Image.new("RGB", (_SIZE, _SIZE), _BG)
    x = (_SIZE - new_w) // 2
    y = (_SIZE - new_h) // 2
    canvas.paste(frame_img, (x, y))
    return canvas



def _current_page(ctx):
    """The page that is on the pad right now, 0 if it cannot be asked.

    The frame files are named after it: two pages can put the same widget on
    the same physical key with different settings, and a name keyed on the key
    index alone meant both wrote to one file, so whichever rendered last
    decided what the other page showed (#88).
    """
    try:
        return int(ctx.get_displaypad_current_page())
    except Exception:
        return 0


def _save_frame(img, path):
    """Write the frame beside its destination and move it into place.

    The upload worker reads these files while we write them, and a PIL save
    straight onto the destination is not atomic: a reader that catches it half
    written gets "cannot identify image file" (#89).
    """
    import os as _os
    tmp = "%s.%d.tmp" % (path, _os.getpid())
    # The format has to be named: PIL takes it from the file extension, and
    # the extension here is .tmp.
    img.save(tmp, "PNG")
    _os.replace(tmp, path)

class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._readers = {}   # key_index -> {"path": str, "stop": Event, "thread": Thread}
        self._players = {}   # key_index -> {"stop": Event, "pause": Event, "thread": Thread, "path": str}
        self._hashes = {}    # key_index -> last pushed image hash

        ctx.register_translations({
            "en": {"dp_video": "Video"},
            "de": {"dp_video": "Video"},
        })
        ctx.register_action_type("dp_video", ctx.T("dp_video"), self.on_press)

        if cv2 is None:
            print("[dp_video] opencv-python is not installed -- "
                  "playback will not work until it is (pip install opencv-python)",
                  flush=True)

    def start(self):
        """Begin watching for keys assigned this action.

        The stop was never cleared here, so once a page switch had stopped
        this plugin the scan thread returned at once and it stayed dead for
        the rest of the session. Each thread carries its own stop now; the
        reader and player threads are told individually by stop(), so they
        are not affected by the change.
        """
        self._stop.set()                # any predecessor ends here
        stop = threading.Event()
        self._stop = stop
        threading.Thread(target=self._scan_loop, args=(stop,),
                         daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._lock:
            for info in self._readers.values():
                info["stop"].set()
            for info in self._players.values():
                info["stop"].set()

    # ------------------------------------------------------------ scanner --
    # Watches the DisplayPad action config for keys assigned the "dp_video"
    # type, and keeps one pipe-reader thread per key in sync with the pipe
    # path the user typed into that key's action field.

    def _scan_loop(self, stop):
        while not stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                # A transient error here must never kill this daemon thread --
                # log and keep scanning, same lesson as dp_clock issue #8.
                try:
                    print(f"[dp_video] scan error (continuing): {e}", flush=True)
                except Exception:
                    pass
            stop.wait(2)

    def _current_actions(self):
        """The 12 button actions of the page that is actually on the pad.

        Not shared.config._load_displaypad_actions(): its page argument
        defaults to 0, so a video assigned on a sub-page was never found while
        the one on Main kept being painted, on whatever key sat at that index
        on the visible page (issues #82 and #70)."""
        try:
            return self.ctx.get_displaypad_actions()
        except Exception:
            pass
        try:
            from shared.config import _load_displaypad_actions
            return _load_displaypad_actions()
        except Exception:
            return []

    def _scan_once(self):
        actions = self._current_actions()
        if not actions:
            return

        assigned = {}
        for i, act in enumerate(actions):
            if act.get("type") != "dp_video":
                continue
            path = act.get("action", "").strip()
            if not path:
                continue
            assigned[i] = os.path.expanduser(path)

        with self._lock:
            for key_index, path in assigned.items():
                current = self._readers.get(key_index)
                if current is not None and current["path"] == path:
                    continue
                if current is not None:
                    # The action field was pointed at a different pipe --
                    # stop the old reader/player and remove the old FIFO
                    # from disk so stale pipes don't pile up.
                    current["stop"].set()
                    self._remove_pipe_file(current["path"])
                    self._stop_player(key_index, clear=False)
                self._start_reader(key_index, path)

            gone = [k for k in self._readers if k not in assigned]
            for k in gone:
                # The action was unassigned/removed from this key -- stop
                # the reader/player and clean up the pipe it was using.
                self._readers[k]["stop"].set()
                self._remove_pipe_file(self._readers[k]["path"])
                self._stop_player(k, clear=False)
                del self._readers[k]

    def _remove_pipe_file(self, path):
        """Best-effort removal of a FIFO we previously created/used."""
        try:
            if path and os.path.exists(path) and stat.S_ISFIFO(os.stat(path).st_mode):
                os.remove(path)
                print(f"[dp_video] removed old pipe {path}", flush=True)
        except Exception as e:
            print(f"[dp_video] could not remove old pipe {path}: {e}", flush=True)

    def _start_reader(self, key_index, path):
        try:
            dirpath = os.path.dirname(path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            if not os.path.exists(path):
                os.mkfifo(path)
            elif not stat.S_ISFIFO(os.stat(path).st_mode):
                print(f"[dp_video] {path} exists and is not a FIFO, skipping", flush=True)
                return
        except Exception as e:
            print(f"[dp_video] could not prepare pipe {path}: {e}", flush=True)
            return

        stop_event = threading.Event()
        t = threading.Thread(
            target=self._reader_loop, args=(path, key_index, stop_event), daemon=True
        )
        self._readers[key_index] = {"path": path, "stop": stop_event, "thread": t}
        t.start()

    # ------------------------------------------------------------- reader --
    # Reads video file paths (one message per pipe open/close cycle, same
    # convention as dp_pipe_text) and hands them off to the player.

    def _reader_loop(self, path, key_index, stop_event):
        while not stop_event.is_set() and not self._stop.is_set():
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                return
            except OSError as e:
                print(f"[dp_video] open failed for {path}: {e}", flush=True)
                stop_event.wait(1)
                continue

            buf = b""
            got_data = False
            try:
                while not stop_event.is_set() and not self._stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        # EOF: writer closed. Reopen for the next writer.
                        break
                    buf += chunk
                    got_data = True
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

            if got_data:
                try:
                    self._handle_message(key_index, buf.decode("utf-8", errors="replace"))
                except Exception as e:
                    print(f"[dp_video] message error for key {key_index + 1}: {e}", flush=True)

    def _handle_message(self, key_index, raw_text):
        text = raw_text.strip()

        if not text or text.lower() == "stop":
            self._stop_player(key_index, clear=True)
            return

        loop = False
        if text.lower().startswith("loop "):
            loop = True
            text = text[5:].strip()

        video_path = os.path.expanduser(text)
        if not os.path.isfile(video_path):
            print(f"[dp_video] file not found: {video_path}", flush=True)
            return

        self._start_player(key_index, video_path, loop)

    # ------------------------------------------------------------- player --

    def _start_player(self, key_index, video_path, loop):
        self._stop_player(key_index, clear=False)

        if cv2 is None:
            print("[dp_video] opencv-python is not installed -- cannot play video", flush=True)
            return

        stop_event = threading.Event()
        pause_event = threading.Event()
        t = threading.Thread(
            target=self._play_video,
            args=(key_index, video_path, loop, stop_event, pause_event),
            daemon=True,
        )
        self._players[key_index] = {
            "stop": stop_event, "pause": pause_event, "thread": t, "path": video_path
        }
        t.start()

    def _stop_player(self, key_index, clear=True):
        player = self._players.pop(key_index, None)
        if player is not None:
            player["stop"].set()
        if clear:
            blank = Image.new("RGB", (_SIZE, _SIZE), _BG)
            self._push_image(key_index, blank, persist=True)

    def _play_video(self, key_index, video_path, loop, stop_event, pause_event):
        while not stop_event.is_set():
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                print(f"[dp_video] cannot open video: {video_path}", flush=True)
                break

            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = 25.0
            frame_time = 1.0 / fps

            try:
                while not stop_event.is_set():
                    if pause_event.is_set():
                        stop_event.wait(0.1)
                        continue

                    start = time.perf_counter()
                    ret, frame = cap.read()
                    if not ret:
                        break  # end of this playthrough

                    try:
                        img = _frame_to_image(frame)
                        self._push_image(key_index, img, persist=False)
                    except Exception as e:
                        print(f"[dp_video] frame render error: {e}", flush=True)

                    elapsed = time.perf_counter() - start
                    sleep = frame_time - elapsed
                    if sleep > 0:
                        stop_event.wait(sleep)
            finally:
                cap.release()

            if not loop or stop_event.is_set():
                break

        # Playback finished (video ended and not looping) or was stopped
        # from the outside. Only clean up/clear if we still own this key's
        # player slot -- a newer video may already have replaced us.
        with self._lock:
            still_ours = self._players.get(key_index, {}).get("stop") is stop_event
            if still_ours:
                del self._players[key_index]
        if still_ours and not stop_event.is_set():
            # Natural end of a non-looping video -- clear back to blank.
            blank = Image.new("RGB", (_SIZE, _SIZE), _BG)
            self._push_image(key_index, blank, persist=True)

    # ----------------------------------------------------------- actions --

    def on_press(self, action_value):
        """Pressing the assigned key pauses/resumes its current video."""
        path = os.path.expanduser((action_value or "").strip())
        with self._lock:
            for key_index, info in self._readers.items():
                if info["path"] != path:
                    continue
                player = self._players.get(key_index)
                if player is not None:
                    if player["pause"].is_set():
                        player["pause"].clear()
                    else:
                        player["pause"].set()
                break

    # -------------------------------------------------------------- push --

    def _push_image(self, key_index, img, persist=True):
        raw = img.tobytes()
        h = hashlib.md5(raw).hexdigest()
        if self._hashes.get(key_index) == h:
            return
        self._hashes[key_index] = h
        self.ctx.push_displaypad_image(key_index, img)

        if not persist:
            # Video frames change constantly -- skip the disk-cache trick
            # below for every frame, it's only meant for static content.
            return

        # Persist the frame to disk and register it as the key's static
        # image, so it survives a page switch/reload -- same trick
        # dp_clock uses for its stopwatch frames.
        try:
            from shared.config import CONFIG_DIR
            page = _current_page(self.ctx)
            img_path = os.path.join(
                CONFIG_DIR, f"dp_video_p{page}_k{key_index}.png")
            _save_frame(img, img_path)
            dp = self.ctx.get_displaypad()
            if dp:
                dp._images[str(key_index)] = img_path
                if hasattr(dp, "_page_images"):
                    dp._page_images.setdefault(page, {})[str(key_index)] = img_path
        except Exception:
            pass
