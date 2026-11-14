"""A private Xvfb for the signed-in browser. No shared display or TCP listener."""
import base64
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time


class JpegSplitter:
    """Keep partial markers between reads; yield only complete JPEGs."""
    def __init__(self):
        self.buffer = b''

    def feed(self, chunk):
        self.buffer += chunk
        while True:
            start = self.buffer.find(b'\xff\xd8')
            if start < 0:
                self.buffer = self.buffer[-1:]
                return
            self.buffer = self.buffer[start:]
            end = self.buffer.find(b'\xff\xd9', 2)
            if end < 0:
                return
            frame, self.buffer = self.buffer[:end + 2], self.buffer[end + 2:]
            yield frame


# Fixed key names only: never let an event supply an xdotool command or option.
KEYSYMS = dict(zip(
    ('Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
     'Home', 'End', 'PageUp', 'PageDown', 'Space', 'Shift', 'Control', 'Alt', 'Meta'),
    ('Return', 'BackSpace', 'Tab', 'Escape', 'Delete', 'Left', 'Right', 'Up', 'Down',
     'Home', 'End', 'Prior', 'Next', 'space', 'Shift_L', 'Control_L', 'Alt_L', 'Super_L')))
KEYSYMS.update({f'F{i}': f'F{i}' for i in range(1, 13)})
KEYSYMS.update({char: char for char in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'})
KEYSYMS.update(dict(zip(
    ' !"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~',
    ('space exclam quotedbl numbersign dollar percent ampersand apostrophe parenleft parenright asterisk plus '
     'comma minus period slash colon semicolon less equal greater question at bracketleft backslash bracketright '
     'asciicircum underscore grave braceleft bar braceright asciitilde').split())))


def _combo(mods):
    """CDP modifier bits as xdotool names. Chrome here runs on Linux, so a Mac's Cmd (Meta) acts as Ctrl:
    Cmd+L, Cmd+A, Cmd+C and Cmd+Z then do what the user expects."""
    names = [name for bit, name in ((1, 'alt'), (2, 'ctrl'), (4, 'ctrl'), (8, 'shift')) if mods & bit]
    return list(dict.fromkeys(names))


class VirtualDisplay:
    size = (2560, 1600)

    def __init__(self, finder=lambda: shutil.which('Xvfb')):
        self.finder = finder
        self.process = None
        self.folder = None
        self.environment = None
        self.capture_lock = threading.RLock()
        self.changed = threading.Condition()
        self.capture = None
        self.reader = None
        self.capture_size = None
        self.viewers = 0
        self.frame = None
        self.seq = 0
        self.capture_ended = True
        self.input_lock = threading.Lock()
        self.held_keys = {}
        self.wheel_rest = [0.0, 0.0]
        self.fitted = False

    @staticmethod
    def _terminate(process):
        if process is None:
            return
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        except ProcessLookupError:
            process.wait(timeout=3)

    def _stop_capture(self, restarting=False):
        process, self.capture = self.capture, None
        self._terminate(process)
        if self.reader:
            self.reader.join(timeout=3)
            self.reader = None
        if process and process.stdout:
            process.stdout.close()
        with self.changed:
            self.frame = None
            if not restarting:
                self.capture_ended = True
                self.changed.notify_all()
        self.capture_size = None

    def _read_capture(self, process, size):
        splitter = JpegSplitter()
        try:
            while chunk := process.stdout.read1(65536):
                for jpeg in splitter.feed(chunk):
                    with self.changed:
                        self.seq += 1
                        self.frame = (self.seq, *size, base64.b64encode(jpeg).decode('ascii'))
                        self.changed.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self.changed:
                if process is self.capture:
                    self.capture_ended = True
                    self.changed.notify_all()

    def watch(self, max_w, max_h):
        # A multiple of 16 makes both sides even at exactly 16:10.
        from browser import _clamp
        width = min(_clamp(max_w, 640, self.size[0], 1280),
                    _clamp(max_h, 400, self.size[1], 800) * 8 // 5)
        width = max(640, width // 16 * 16)
        size = (width, width * 5 // 8)
        with self.capture_lock:
            executable = shutil.which('ffmpeg')
            if not executable:
                raise RuntimeError('ffmpeg is not installed.')
            # Keep the largest requested size for this watching interval; unwatch needs no viewer identity.
            if self.capture_size:
                size = max(size, self.capture_size)
            if self.capture is None or self.capture.poll() is not None or size != self.capture_size:
                self._stop_capture(restarting=True)
                try:
                    self.capture = subprocess.Popen(
                        [executable, '-loglevel', 'error', '-f', 'x11grab', '-draw_mouse', '1', '-framerate', '10',
                         '-video_size', '%dx%d' % self.size, '-i', self.environment['DISPLAY'],
                         '-vf', 'scale=%d:%d' % size, '-q:v', '6', '-f', 'image2pipe', '-c:v', 'mjpeg', '-'],
                        env=dict(os.environ, **self.environment), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
                except OSError as exc:
                    with self.changed:
                        self.capture_ended = True
                        self.changed.notify_all()
                    raise RuntimeError('Could not capture the Chrome window: ' + str(exc)) from exc
                self.capture_size = size
                with self.changed:
                    self.capture_ended = False
                self.reader = threading.Thread(target=self._read_capture, args=(self.capture, size),
                                               daemon=True, name='browser-window-capture')
                self.reader.start()
            self.viewers += 1
        self._fit_window()

    def _fit_window(self):
        """Chrome keeps the window place it saved in the profile (it opened at 20,20), so move the main window to
        fill the display. Once per display start, and only the largest window: popups such as a sign-in window
        keep their own size and place."""
        executable = shutil.which('xdotool')
        if self.fitted or not executable or not self.environment:
            return
        env = dict(os.environ, **self.environment)
        try:
            found = subprocess.run([executable, 'search', '--onlyvisible', '--class', 'google-chrome'], env=env,
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
            best, area = None, 0
            for window in found.stdout.split():
                if not window.isdigit():
                    continue
                shape = subprocess.run([executable, 'getwindowgeometry', '--shell', window], env=env,
                                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
                values = dict(line.split('=', 1) for line in shape.stdout.splitlines() if '=' in line)
                size = int(values.get('WIDTH', '0') or 0) * int(values.get('HEIGHT', '0') or 0)
                if size > area:
                    best, area = window, size
            if best is None:
                return  # no window yet: try again on the next watch
            subprocess.run([executable, 'windowmove', best, '0', '0', 'windowsize', best, str(self.size[0]), str(self.size[1])],
                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5)
            self.fitted = True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # a misplaced window is still usable

    def unwatch(self):
        with self.capture_lock:
            self.viewers = max(0, self.viewers - 1)
            if not self.viewers:
                self._stop_capture()

    def wait_frame(self, after_seq, timeout):
        with self.changed:
            self.changed.wait_for(lambda: (self.frame and self.frame[0] > after_seq) or self.capture_ended, timeout)
            if self.frame and self.frame[0] > after_seq:
                return self.frame
            if self.capture_ended:
                raise RuntimeError('The Chrome window capture stopped.')
            return None

    def input(self, events):
        with self.input_lock:
            return self._input(events)

    def _input(self, events):
        from browser import input_command
        if not isinstance(events, list) or len(events) > 200:
            raise ValueError('Invalid input.')
        args = []
        held = dict(self.held_keys)
        wheel = list(self.wheel_rest)
        for event in events:
            _, params = input_command(event)
            kind = event['type']
            if kind in ('down', 'up', 'move', 'wheel'):
                args += ['mousemove', str(int(max(0, min(self.size[0] - 1, params['x'])))),
                         str(int(max(0, min(self.size[1] - 1, params['y']))))]
                if kind in ('down', 'up'):
                    button = {'left': 1, 'middle': 2, 'right': 3}.get(params['button'])
                    if button:
                        args += ['mousedown' if kind == 'down' else 'mouseup', str(button)]
                elif kind == 'wheel':
                    # One X wheel click scrolls about 100 px. Keep the rest for the next batch, so a trackpad's
                    # many small deltas add up instead of each becoming a full click.
                    for axis, delta, negative, positive in ((1, params['deltaY'], 4, 5), (0, params['deltaX'], 6, 7)):
                        total = wheel[axis] + delta
                        clicks = max(-10, min(10, int(total / 100)))
                        wheel[axis] = max(-100.0, min(100.0, total - clicks * 100))
                        if clicks:
                            args += ['click', str(negative if clicks < 0 else positive)] * abs(clicks)
            elif kind == 'text':
                if '\0' in params['text']:
                    raise ValueError('Invalid text.')
                # type otherwise consumes the remaining commands as text (xdotool cmd_type.c).
                args += ['type', '--delay', '0', '--args', '1', '--', params['text']]
            else:
                key = params['key']
                symbol = KEYSYMS.get(key)
                printable = len(key) == 1 and key.isprintable()
                mods = params['modifiers']
                if printable:
                    if kind == 'keyup':
                        continue  # type/key already send both press and release
                    if not mods & 7:
                        args += ['type', '--delay', '0', '--args', '1', '--', key]
                    elif symbol:
                        args += ['key', '+'.join([*_combo(mods), symbol])]
                elif symbol:
                    combo = [] if key in ('Alt', 'Control', 'Meta', 'Shift') else _combo(mods)
                    chord = '+'.join([*combo, symbol])
                    if kind == 'keydown':
                        if key in held and held[key] != chord:
                            args += ['keyup', held[key]]
                        held[key] = chord
                    else:
                        chord = held.pop(key, symbol)
                    args += [kind, chord]
        executable = shutil.which('xdotool')
        if not executable:
            raise RuntimeError('xdotool is not installed.')
        if args:
            try:
                subprocess.run([executable, *args], env=dict(os.environ, **self.environment),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=5, check=True)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError('Could not send input to the Chrome window.') from exc
        self.held_keys = held
        self.wheel_rest = wheel
        return {'ok': True}

    @staticmethod
    def _authority(path, number, cookie):
        # FamilyWild avoids hostname differences. The server reads the cookie before displayfd selects a number;
        # the client file then gets that exact display number. No xauth dependency or access-control bypass.
        fields = (b'', number.encode(), b'MIT-MAGIC-COOKIE-1', cookie)
        path.write_bytes(struct.pack('!H', 65535) + b''.join(struct.pack('!H', len(f)) + f for f in fields))
        path.chmod(0o600)

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return self.environment
        self.stop()
        executable = self.finder()
        if not executable:
            raise RuntimeError('Xvfb is not installed.')
        self.folder = tempfile.TemporaryDirectory(prefix='tether-signed-display-')
        authority = Path(self.folder.name) / 'Xauthority'
        cookie = secrets.token_bytes(16)
        self._authority(authority, '', cookie)
        read_fd, write_fd = os.pipe()
        try:
            self.process = subprocess.Popen(
                [executable, '-displayfd', str(write_fd), '-screen', '0', '%dx%dx24' % self.size,
                 '-nolisten', 'tcp', '-noreset', '-auth', str(authority)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(write_fd,), start_new_session=True)
            os.close(write_fd)
            write_fd = None
            deadline = time.monotonic() + 10
            number = b''
            while b'\n' not in number and time.monotonic() < deadline:
                ready, _, _ = select.select([read_fd], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                chunk = os.read(read_fd, 32)
                if not chunk:
                    break
                number += chunk
            if not number.strip().isdigit() or b'\n' not in number or self.process.poll() is not None:
                raise RuntimeError('The signed-in browser display did not start.')
            number = number.strip().decode('ascii')
            self._authority(authority, number, cookie)
            self.environment = {'DISPLAY': ':' + number, 'XAUTHORITY': str(authority)}
            return self.environment
        except (OSError, RuntimeError):
            self.stop()
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def stop(self):
        with self.capture_lock:
            # Outstanding streams still owe an unwatch. Keep their registrations across a browser restart,
            # or an old stream's cleanup could stop the new viewer's capture.
            self._stop_capture()
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        except ProcessLookupError:
            process.wait(timeout=3)
        finally:
            if self.folder:
                self.folder.cleanup()
                self.folder = None
            self.environment = None
        self.held_keys = {}
        self.wheel_rest = [0.0, 0.0]
        self.fitted = False  # a new Chrome window opens at its saved place
