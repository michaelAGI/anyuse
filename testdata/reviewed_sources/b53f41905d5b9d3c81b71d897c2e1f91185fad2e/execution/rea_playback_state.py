"""Independent, bounded public-control and temporal display-pixel observations.

No app methods, activation, AX actions, permissions, or model are used.  A
frozen observer declares the public control and video region.  Current media
and window identities are case context, never actor arguments.  Retained PNGs
are decoded again by the historical consumer; supplied pixel statistics are
not trusted.  These unsigned local measurements are not authentication.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import io
import math
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit

from . import rea_ax_state as S
from . import rea_mapping as R
from . import rea_state_evidence as E

OBSERVER = "anyuse.rea-temporal-playback-observer/1"
CONTEXT = "anyuse.rea-playback-context/1"
SNAPSHOT = "anyuse.rea-playback-public-snapshot/1"
OBSERVATION = "anyuse.rea-temporal-playback-observation/1"
MAX_PNG_BYTES, MAX_PIXELS, MAX_MEDIA_BYTES = 64 * 1024**2, 16 * 1024**2, 512 * 1024**2
MAX_ALPHA_BYTES, MAX_ALPHA_WINDOWS = 512 * 1024, 4
SAMPLE_SIZE = (96, 54)


def _need(value, message):
    if not value: raise ValueError(message)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed playback schema required")


def _number(value, lo=0, hi=1e12):
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi


def _text(value, *, empty=False):
    return type(value) is str and (empty or bool(value)) and len(value.encode()) <= 4096 and all(ord(c) >= 32 and ord(c) != 127 for c in value)


def validate_observer(value):
    value = copy.deepcopy(value)
    _keys(value, {"format", "control", "video"})
    _need(value["format"] == OBSERVER, "versioned independent playback observer required")
    control, video = value["control"], value["video"]
    _keys(control, {"path", "role", "identifier", "playing_title", "paused_title"})
    _need(type(control["path"]) is list and 1 <= len(control["path"]) <= 12, "bounded explicit public AX control path required")
    for step in control["path"]:
        _keys(step, {"attribute", "index"})
        _need(step["attribute"] in {"AXMenuBar", "AXChildren"} and
              (step["index"] is None if step["attribute"] == "AXMenuBar" else
               type(step["index"]) is int and 0 <= step["index"] < 128), "only bounded public AX relations are supported")
    _need(control["role"] in {"AXMenuItem", "AXButton"} and
          (control["identifier"] is None or _text(control["identifier"], empty=True)) and
          _text(control["playing_title"]) and _text(control["paused_title"]) and
          control["playing_title"] != control["paused_title"], "distinct exact public playback-control states required")
    _keys(video, {"region", "frames", "interval_seconds", "black_luma", "min_nonblack_fraction", "motion_min", "still_max"})
    region = video["region"]
    _need(type(region) is list and len(region) == 4 and all(_number(x, 0, 1) for x in region) and
          region[2] > 0 and region[3] > 0 and region[0] + region[2] <= 1 and region[1] + region[3] <= 1,
          "video region must be a positive normalized rectangle inside the selected window")
    _need(type(video["frames"]) is int and 3 <= video["frames"] <= 8 and
          _number(video["interval_seconds"], .1, 2) and (video["frames"] - 1) * video["interval_seconds"] <= 12 and
          type(video["black_luma"]) is int and 0 <= video["black_luma"] <= 254 and
          _number(video["min_nonblack_fraction"], .001, 1) and
          _number(video["motion_min"], .01, 255) and _number(video["still_max"], 0, 255) and
          video["still_max"] < video["motion_min"], "bounded separated temporal/nonblack thresholds required")
    return value


def _window(window):
    _keys(window, {"id", "pid", "role", "subrole", "title", "document", "position", "size"})
    _need(type(window["id"]) is int and window["id"] > 0 and type(window["pid"]) is int and window["pid"] > 0 and
          window["role"] == "AXWindow" and (window["subrole"] is None or _text(window["subrole"])) and
          _text(window["title"], empty=True) and _text(window["document"]), "complete exact public window identity required")
    for key in ("position", "size"):
        _need(type(window[key]) is list and len(window[key]) == 2 and
              all(_number(v, -32768 if key == "position" else .01, 32768) for v in window[key]), "bounded actual window geometry required")
    return copy.deepcopy(window)


def _display(display):
    _keys(display, {"id", "position", "size"})
    _need(type(display["id"]) is int and display["id"] > 0, "exact display identity required")
    for key in ("position", "size"):
        _need(type(display[key]) is list and len(display[key]) == 2 and
              all(_number(v, -32768 if key == "position" else .01, 32768) for v in display[key]), "bounded display geometry required")
    return copy.deepcopy(display)


def _video_bounds(window, video):
    rx, ry, rw, rh = video["region"]
    x, y = window["position"]; width, height = window["size"]
    return x + rx * width, y + ry * height, x + (rx + rw) * width, y + (ry + rh) * height


def _overlap(row, bounds):
    left, top, right, bottom = bounds
    wx, wy = row["position"]; ww, wh = row["size"]
    return min(right, wx + ww) > max(left, wx) and min(bottom, wy + wh) > max(top, wy)


def _alpha_pixels(raw, row, bounds, scale):
    """Only actual retained per-window alpha can disprove rectangle occlusion."""
    from PIL import Image
    _need(type(raw) is bytes and 45 <= len(raw) <= MAX_ALPHA_BYTES and raw[:8] == b"\x89PNG\r\n\x1a\n",
          "bounded actual occluder PNG required")
    # Pillow exposes 16-bit RGBA as 8-bit RGBA, which can round a small
    # nonzero alpha to zero. Accept only losslessly decoded explicit 8-bit A.
    _need(raw[8:12] == b"\x00\x00\x00\r" and raw[12:16] == b"IHDR" and raw[24] == 8 and raw[25] in {4, 6},
          "actual occluder PNG requires explicit 8-bit alpha without decoder precision loss")
    with Image.open(io.BytesIO(raw)) as image:
        _need(image.format == "PNG" and image.mode in {"RGBA", "LA"} and "A" in image.getbands() and
              not getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) == 1 and
              0 < image.width * image.height <= MAX_PIXELS, "actual occluder PNG must contain one complete explicit alpha frame")
        image.load()
        sx, sy = image.width / row["size"][0], image.height / row["size"][1]
        _need(abs(sx - scale) < 1e-6 and abs(sy - scale) < 1e-6,
              "occluder capture scale differs from exact window bounds and observed display scale")
        wx, wy = row["position"]; ww, wh = row["size"]
        left, top, right, bottom = bounds
        # Cover every pixel touched by the geometric intersection. Rounding
        # inward could miss a partly covered edge pixel.
        box = [math.floor((max(left, wx) - wx) * sx), math.floor((max(top, wy) - wy) * sy),
               math.ceil((min(right, wx + ww) - wx) * sx), math.ceil((min(bottom, wy + wh) - wy) * sy)]
        _need(0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height,
              "occluder alpha crop differs from the bound video/window intersection")
        _need(image.getchannel("A").crop(box).getextrema() == (0, 0),
              "an onscreen window above the owned video overlaps the frozen video region with nonzero alpha")


def _alpha_evidence(capture, row, rows, bounds, scale, window):
    _keys(capture, {"window_id", "started_monotonic", "finished_monotonic", "before", "after", "png"})
    _need(capture["window_id"] == row["id"] and type(capture["window_id"]) is int and
          S._same(_relevant_inventory(capture["before"], window, bounds), _relevant_inventory(rows, window, bounds)) and
          S._same(_relevant_inventory(capture["after"], window, bounds), _relevant_inventory(rows, window, bounds)),
          "occluder capture requires unchanged selected window and relevant Quartz occlusion bookends")
    start, finish = E._moment(capture["started_monotonic"]), E._moment(capture["finished_monotonic"])
    _need(start < finish and finish - start <= 8, "bounded actual occluder capture interval required")
    png = capture["png"]
    _keys(png, {"base64", "sha256", "bytes"})
    _need(type(png["base64"]) is str and len(png["base64"]) <= 4 * ((MAX_ALPHA_BYTES + 2) // 3) and
          type(png["bytes"]) is int and 45 <= png["bytes"] <= MAX_ALPHA_BYTES and
          type(png["sha256"]) is str and R.SHA.fullmatch(png["sha256"]), "bounded pinned actual occluder PNG required")
    try: raw = base64.b64decode(png["base64"], validate=True)
    except (ValueError, TypeError) as error: raise ValueError("canonical occluder PNG base64 required") from error
    _need(base64.b64encode(raw).decode("ascii") == png["base64"] and len(raw) == png["bytes"] and
          hashlib.sha256(raw).hexdigest() == png["sha256"], "retained occluder PNG bytes differ from exact pin")
    _alpha_pixels(raw, row, bounds, scale)
    return len(raw)


def _relevant_inventory(rows, window, bounds):
    """Validate the complete inventory; project only binding/ROI-relevant rows.

    Capturing pixels can itself add off-region status surfaces or change an
    unrelated window's title. They stay in the evidence, while every inventory
    independently rechecks selected identity, ambiguity and foreground order.
    """
    _need(type(rows) is list and 1 <= len(rows) <= 512, "bounded complete visible-window inventory required")
    identifiers = set()
    for row in rows:
        _keys(row, {"id", "pid", "layer", "alpha", "on_screen", "position", "size", "title"})
        _need(type(row["id"]) is int and row["id"] > 0 and row["id"] not in identifiers and
              type(row["pid"]) is int and row["pid"] >= 0 and type(row["layer"]) is int and
              _number(row["alpha"], 0, 1) and row["on_screen"] is True and
              (row["title"] is None or _text(row["title"], empty=True)), "complete typed visible-window metadata required")
        identifiers.add(row["id"])
        for key in ("position", "size"):
            _need(type(row[key]) is list and len(row[key]) == 2 and
                  all(_number(v, -32768 if key == "position" else .000001, 32768) for v in row[key]), "finite visible-window bounds required")
    selected = [row for row in rows if row["id"] == window["id"]]
    _need(len(selected) == 1, "selected public window is absent from Quartz inventory")
    selected = selected[0]
    _need(all(S._same(selected[k], window[k]) for k in ("id", "pid", "position", "size")) and
          selected["layer"] == 0 and selected["alpha"] > 0, "Quartz selected window differs from exact public AX window")
    ordinary = [row for row in rows if row["layer"] == 0 and row["alpha"] > 0 and all(v > 80 for v in row["size"])]
    matched = _match_public_window(ordinary, window)
    _need(matched["id"] == selected["id"], "historical Quartz match differs from the selected public window")
    return {"selected": selected, "overlaps": [row for row in rows[:rows.index(selected)] if _overlap(row, bounds)]}


def _visibility(value, window, video):
    """Recompute binding/overlap from complete front-to-back Quartz metadata."""
    _keys(value, {"selected", "above", "below", "capture_scale", "alpha_captures"})
    _need(type(value["capture_scale"]) is int and 1 <= value["capture_scale"] <= 8,
          "bounded exact observed display backing scale required")
    _need(type(value["above"]) is list and type(value["below"]) is list and
          len(value["above"]) + len(value["below"]) < 512, "bounded complete visible-window inventory required")
    rows = [*value["above"], value["selected"], *value["below"]]
    bounds = _video_bounds(window, video)
    relevant = _relevant_inventory(rows, window, bounds)
    _need(S._same(relevant["selected"], value["selected"]), "selected Quartz inventory split differs")
    overlaps = [row for row in relevant["overlaps"] if row["alpha"] > 0]
    captures = value["alpha_captures"]
    _need(type(captures) is list and len(captures) == len(overlaps) <= MAX_ALPHA_WINDOWS,
          "every onscreen window that overlaps the video requires bounded actual alpha evidence")
    total = sum(_alpha_evidence(capture, row, rows, bounds, value["capture_scale"], window) for capture, row in zip(captures, overlaps))
    _need(total <= MAX_ALPHA_BYTES, "aggregate occluder PNG byte budget exceeded")


def _stable_snapshot(value, video):
    """Keep exact public state and all identity/occlusion-relevant CG rows."""
    value = copy.deepcopy(value)
    visibility = value["visibility"]
    relevant = _relevant_inventory([*visibility["above"], visibility["selected"], *visibility["below"]],
                                   value["window"], _video_bounds(value["window"], video))
    value["visibility"] = {**relevant, "capture_scale": visibility["capture_scale"]}
    return value


def _alpha_interval(snapshot, start, finish):
    _need(all(start <= capture["started_monotonic"] < capture["finished_monotonic"] <= finish
              for capture in snapshot["visibility"]["alpha_captures"]),
          "occluder captures lie outside their actual public snapshot interval")


def _document_basename(document):
    parsed = urlsplit(document)
    path = Path(unquote(parsed.path))
    _need(parsed.scheme == "file" and parsed.netloc == "" and not parsed.query and not parsed.fragment and
          path.is_absolute() and ".." not in path.parts and path.as_uri() == document and _text(path.name),
          "canonical local public document URI required for Quartz title binding")
    return path.name


def _match_public_window(ordinary, window):
    # Standard document windows may expose a decorated AX title while Quartz
    # exposes the document filename. Accept either exact public title or exact
    # canonical document basename; never parse an application-specific delimiter.
    _need(_text(window["title"], empty=True) and ordinary, "literal focused AX title and visible ordinary windows required")
    titles = {window["title"], _document_basename(window["document"])}
    matches = []
    for row in ordinary:
        if row["pid"] != window["pid"] or any(not S._same(row[k], window[k]) for k in ("position", "size")): continue
        _need(_text(row["title"]), "literal nonempty Quartz title unavailable for an owned matching-geometry window")
        if row["title"] in titles: matches.append(row)
    _need(len(matches) == 1, "public title/document and exact geometry must identify one unique owned Quartz window")
    _need(ordinary[0]["id"] == matches[0]["id"],
          "the uniquely matched public window must be the foremost ordinary window")
    return matches[0]


def _select_owned_window(ordinary, identity, position, size, title, document, keys):
    normalized = [{"id": int(row[keys.kCGWindowNumber]), "pid": int(row[keys.kCGWindowOwnerPID]),
                   "title": S._scalar(row.get(keys.kCGWindowName)),
                   "position": [float(row[keys.kCGWindowBounds][key]) for key in ("X", "Y")],
                   "size": [float(row[keys.kCGWindowBounds][key]) for key in ("Width", "Height")]} for row in ordinary]
    selected = _match_public_window(normalized, {"pid": identity["pid"], "position": position, "size": size,
                                                "title": title, "document": document})
    return ordinary[normalized.index(selected)]


def validate_context(value):
    value = copy.deepcopy(value)
    _keys(value, {"format", "window", "media"})
    _need(value["format"] == CONTEXT, "versioned current-media context required")
    _window(value["window"])
    pin = value["media"]
    _keys(pin, {"file", "sha256", "bytes"})
    name = pin["file"]
    _need(_text(name) and name.startswith("/") and str(Path(name)) == name and ".." not in Path(name).parts and not name.startswith("//") and
          type(pin["sha256"]) is str and R.SHA.fullmatch(pin["sha256"]) and
          type(pin["bytes"]) is int and 0 < pin["bytes"] <= MAX_MEDIA_BYTES, "exact bounded local media pin required")
    _need(value["window"]["document"] == Path(name).as_uri(), "public document must identify the exact pinned local media")
    return value


def _system(value, identity):
    """Unknown global AX focus stays unknown; exact local window is checked below."""
    _keys(value, {"identity", "kernel", "session", "focus"})
    _need(S._same(S.validate_identity(value["identity"]), identity), "playback process epoch changed")
    kernel, session, focus = value["kernel"], value["session"], value["focus"]
    _keys(kernel, {"pid", "ppid", "uid", "started_at", "executable"})
    _need(all(type(kernel[k]) is int and kernel[k] >= 0 for k in ("pid", "ppid", "uid")) and
          kernel["pid"] == identity["pid"] and kernel["uid"] == identity["uid"] and
          kernel["executable"] == identity["executable"] and _text(kernel["started_at"]), "actual matching kernel epoch required")
    _keys(session, {"asid", "attribute_bits", "uid", "on_console", "login_done", "locked", "ax_trusted"})
    _need(type(session["asid"]) is int and session["asid"] > 0 and type(session["attribute_bits"]) is int and
          session["attribute_bits"] & 0x10 and not session["attribute_bits"] & 0x1000 and
          session["uid"] == identity["uid"] and type(session["uid"]) is int and
          session["on_console"] is True and session["login_done"] is True and session["locked"] is False and session["ax_trusted"] is True,
          "existing authorized unlocked same-user GUI context required")
    _keys(focus, {"frontmost_pid", "focused_pid"})
    _need(type(focus["frontmost_pid"]) is int and focus["frontmost_pid"] == identity["pid"] and
          (focus["focused_pid"] is None or type(focus["focused_pid"]) is int and focus["focused_pid"] == identity["pid"]),
          "exact owned app must already be foreground; no activation or contradictory AX focus")
    return copy.deepcopy(value)


def validate_snapshot(value, observer, identity, context=None):
    observer, identity = validate_observer(observer), S.validate_identity(identity)
    _keys(value, {"format", "system", "window", "display", "displays", "visibility", "control"})
    _need(value["format"] == SNAPSHOT, "actual public playback snapshot required")
    _system(value["system"], identity)
    window, display = _window(value["window"]), _display(value["display"])
    _need(type(value["displays"]) is list and len(value["displays"]) == 1 and
          S._same(_display(value["displays"][0]), display), "this display-capture backend requires exactly one observed display")
    _need(window["pid"] == identity["pid"], "selected window belongs to another process")
    _visibility(value["visibility"], window, observer["video"])
    for axis in (0, 1):
        _need(display["position"][axis] <= window["position"][axis] and
              window["position"][axis] + window["size"][axis] <= display["position"][axis] + display["size"][axis],
              "selected window must be fully contained on one observed display")
    control, expected = value["control"], observer["control"]
    _keys(control, {"role", "identifier", "title", "enabled"})
    _need(control["role"] == expected["role"] and control["identifier"] == expected["identifier"] and
          control["title"] in {expected["playing_title"], expected["paused_title"]} and control["enabled"] is True,
          "specified independent public control unavailable or contradictory")
    if context is not None:
        context = validate_context(context)
        _need(S._same(window, context["window"]), "focused main window/media changed within trial")
    return copy.deepcopy(value)


def image_metrics(raw, window, display, video):
    """Decode actual full-display PNG bytes, derive the frozen crop ourselves."""
    from PIL import Image, ImageStat
    _need(type(raw) is bytes and 45 <= len(raw) <= MAX_PNG_BYTES and raw[:8] == b"\x89PNG\r\n\x1a\n", "bounded actual display PNG required")
    _window(window); _display(display)
    with Image.open(io.BytesIO(raw)) as image:
        _need(image.format == "PNG" and 0 < image.width * image.height <= MAX_PIXELS, "bounded complete display PNG dimensions required")
        image.load()
        sx, sy = image.width / display["size"][0], image.height / display["size"][1]
        _need(abs(sx - sy) < 1e-6, "display capture scale differs across axes")
        x = (window["position"][0] - display["position"][0]) * sx
        y = (window["position"][1] - display["position"][1]) * sy
        width, height = window["size"][0] * sx, window["size"][1] * sy
        rx, ry, rw, rh = video["region"]
        box = [round(x + rx * width), round(y + ry * height), round(x + (rx + rw) * width), round(y + (ry + rh) * height)]
        _need(0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height, "video crop is outside the actual display capture")
        crop = image.convert("RGB").crop(box).resize(SAMPLE_SIZE, Image.Resampling.BILINEAR)
        pixels = crop.tobytes()
        fraction = sum((r + g + b) / 3 > video["black_luma"] for r, g, b in crop.getdata()) / (SAMPLE_SIZE[0] * SAMPLE_SIZE[1])
        return {"display_size": [image.width, image.height], "crop_box": box,
                "sample_size": list(SAMPLE_SIZE), "sample_sha256": hashlib.sha256(pixels).hexdigest(),
                "mean_rgb": ImageStat.Stat(crop).mean, "nonblack_fraction": fraction}, pixels


def temporal_metrics(images):
    _need(len(images) >= 3 and all(type(x) is bytes and len(x) == SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 3 for x in images), "complete equal-size temporal pixels required")
    return [sum(abs(a - b) for a, b in zip(left, right)) / len(left) for left, right in zip(images, images[1:])]


def validate_observation(value, observer, target, identity, context, state, reader):
    observer, target, identity, context = validate_observer(observer), S.validate_target(target), S.validate_identity(identity), validate_context(context)
    _keys(value, {"format", "target", "identity", "context", "state", "started_monotonic", "finished_monotonic", "samples", "deltas", "read_only", "proof_created"})
    _need(value["format"] == OBSERVATION and value["read_only"] is True and value["proof_created"] is False and
          S._same(value["target"], target) and S._same(value["identity"], identity) and S._same(value["context"], context) and
          value["state"] == state and state in {"playing", "paused"}, "exact independent playback observation binding required")
    start, finish = E._moment(value["started_monotonic"]), E._moment(value["finished_monotonic"])
    _need(start < finish and type(value["samples"]) is list and len(value["samples"]) == observer["video"]["frames"], "complete ordered temporal window required")
    previous, images, common = None, [], None
    for sample in value["samples"]:
        _keys(sample, {"started_monotonic", "capture_started_monotonic", "capture_finished_monotonic", "finished_monotonic", "before", "after", "image", "metrics"})
        moments = [E._moment(sample[k]) for k in ("started_monotonic", "capture_started_monotonic", "capture_finished_monotonic", "finished_monotonic")]
        _need(start <= moments[0] <= moments[1] < moments[2] <= moments[3] <= finish and
              (previous is None or moments[1] - previous >= observer["video"]["interval_seconds"]), "actual frame ordering/spacing differs")
        before = validate_snapshot(sample["before"], observer, identity, context)
        after = validate_snapshot(sample["after"], observer, identity, context)
        _alpha_interval(before, moments[0], moments[1])
        _alpha_interval(after, moments[2], moments[3])
        _need(S._same(_stable_snapshot(before, observer["video"]), _stable_snapshot(after, observer["video"])) and
              (common is None or S._same(_stable_snapshot(before, observer["video"]), _stable_snapshot(common, observer["video"]))),
              "process/kernel/window/display/control changed during temporal observation")
        expected_title = observer["control"]["playing_title" if state == "playing" else "paused_title"]
        _need(before["control"]["title"] == expected_title, "public playback control contradicts expected state")
        raw = reader.read(sample["image"], "playback_pixels", maximum=MAX_PNG_BYTES)
        metrics, pixels = image_metrics(raw, before["window"], before["display"], observer["video"])
        _need(all(abs(metrics["display_size"][axis] - before["display"]["size"][axis] * before["visibility"]["capture_scale"]) < 1e-6
                  for axis in (0, 1)), "actual display PNG differs from the observed backing scale")
        _need(S._same(metrics, sample["metrics"]) and metrics["nonblack_fraction"] >= observer["video"]["min_nonblack_fraction"], "actual video crop is black or its retained statistics were changed")
        previous, common = moments[1], before
        images.append(pixels)
    deltas = temporal_metrics(images)
    _need(S._same(value["deltas"], deltas), "temporal deltas differ from actual retained pixels")
    _need(all(d >= observer["video"]["motion_min"] for d in deltas) if state == "playing" else
          all(d <= observer["video"]["still_max"] for d in deltas), "actual temporal video contradicts public playback state")
    return {"kernel": copy.deepcopy(common["system"]["kernel"]), "system": copy.deepcopy(common["system"]),
            "window": copy.deepcopy(common["window"]), "started_monotonic": start, "finished_monotonic": finish,
            "deltas": deltas}


def capture(observer, target, identity, context, state, out, *, deadline):
    R.assert_guest()
    return _capture(observer, target, identity, context, state, out, deadline=deadline, backend=_Mac(), clock=time.monotonic, sleep=time.sleep)


def check_current(observer, identity, context, *, deadline):
    """Fresh no-action public precondition immediately before B preparation.

    This narrows the dispatch gap; separate OS/AX/Frida calls are not atomic.
    """
    R.assert_guest()
    observer, identity, context = validate_observer(observer), S.validate_identity(identity), validate_context(context)
    snapshot = validate_snapshot(_Mac().inspect(identity, observer, deadline), observer, identity, context)
    _need(snapshot["control"]["title"] == observer["control"]["playing_title"], "current playback is no longer publicly playing")
    return snapshot


def _capture(observer, target, identity, context, state, out, *, deadline, backend, clock, sleep):
    observer, target, identity, context = validate_observer(observer), S.validate_target(target), S.validate_identity(identity), validate_context(context)
    _need(state in {"playing", "paused"} and _number(deadline) and deadline > clock(), "bounded prospective playback observation required")
    folder = Path(out).resolve(); folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    value = {"format": OBSERVATION, "target": target, "identity": identity, "context": context, "state": state,
             "started_monotonic": clock(), "finished_monotonic": None, "samples": [], "deltas": [], "read_only": True, "proof_created": False}
    images, previous = [], None
    for index in range(observer["video"]["frames"]):
        if previous is not None:
            delay = max(0, previous + observer["video"]["interval_seconds"] - clock())
            _need(clock() + delay < deadline, "temporal observation deadline exhausted")
            sleep(delay)
        _need(clock() < deadline, "temporal observation deadline exhausted")
        sample = {"started_monotonic": clock(), "before": backend.inspect(identity, observer, deadline)}
        validate_snapshot(sample["before"], observer, identity, context)
        file = folder / f"frame-{index:02d}-display.png"
        sample["capture_started_monotonic"] = previous = clock()
        backend.frame(sample["before"]["display"], file, deadline)
        sample["capture_finished_monotonic"] = clock()
        sample["after"] = backend.inspect(identity, observer, deadline)
        sample["finished_monotonic"] = clock()
        raw = file.read_bytes()
        sample["image"] = {"file": str(file), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        sample["metrics"], pixels = image_metrics(raw, sample["before"]["window"], sample["before"]["display"], observer["video"])
        images.append(pixels); value["samples"].append(sample)
        R.write_json(folder / "partial-observation.json", value)
    value["finished_monotonic"], value["deltas"] = clock(), temporal_metrics(images)
    _need(value["finished_monotonic"] <= deadline, "temporal observation completed after deadline")
    validate_observation(value, observer, target, identity, context, state, E._Reader())
    R.write_json(folder / "observation.json", value)
    return value


class _Mac:
    """Fixed lazy public APIs. No injected callback, framework path, or actor."""
    def __init__(self):
        self.ax = S._Mac()

    def _get(self, node, attribute, identity, deadline, *, optional=False):
        _need(time.monotonic() < deadline and self.ax.owner(node) == identity["pid"], "bounded same-owner AX read required")
        self.ax.timeout(node, min(S.AX_SECONDS, deadline - time.monotonic()))
        code, value = self.ax.attribute(node, attribute)
        if optional and code in {-25205, -25212}: return None
        _need(code == 0 and value is not None, "required public AX attribute unavailable: " + attribute)
        return value

    def _inventory(self, deadline):
        Q = self.ax.Q
        _need(time.monotonic() < deadline, "visible-window inventory deadline exhausted")
        visible = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements, Q.kCGNullWindowID)
        _need(visible is not None and len(visible) <= 512, "bounded actual visible-window inventory required")
        return visible

    def _visibility_row(self, row):
        Q = self.ax.Q
        b = row[Q.kCGWindowBounds]
        return {"id": int(row[Q.kCGWindowNumber]), "pid": int(row[Q.kCGWindowOwnerPID]), "layer": int(row[Q.kCGWindowLayer]),
                "alpha": float(row[Q.kCGWindowAlpha]), "on_screen": bool(row[Q.kCGWindowIsOnscreen]),
                "title": S._scalar(row.get(Q.kCGWindowName)),
                "position": [float(b["X"]), float(b["Y"])], "size": [float(b["Width"]), float(b["Height"])]}

    def _alpha_captures(self, visibility, window, video, deadline):
        """Read actual transparency without any owner/name/layer exemptions.

        Metadata and pixels are sequential bookended observations, not an
        atomic compositor guarantee. Every unexplained overlap still refuses.
        """
        rows = [*visibility["above"], visibility["selected"], *visibility["below"]]
        bounds = _video_bounds(window, video)
        overlaps = [row for row in visibility["above"] if row["alpha"] > 0 and _overlap(row, bounds)]
        _need(len(overlaps) <= MAX_ALPHA_WINDOWS, "too many overlapping windows for bounded alpha evidence")
        if not overlaps: return []
        _need(self.ax.Q.CGPreflightScreenCaptureAccess(), "existing Screen Recording permission required; no prompt")
        captures, total = [], 0
        with tempfile.TemporaryDirectory(prefix="anyuse-playback-alpha-") as temporary:
            for index, row in enumerate(overlaps):
                _need(time.monotonic() < deadline, "occluder capture deadline exhausted")
                capture = {"window_id": row["id"], "started_monotonic": time.monotonic(),
                           "before": [self._visibility_row(item) for item in self._inventory(deadline)]}
                _need(S._same(_relevant_inventory(capture["before"], window, bounds), _relevant_inventory(rows, window, bounds)),
                      "selected window or relevant Quartz occlusion changed before capture")
                file = Path(temporary) / f"occluder-{index}.png"
                remaining = min(8 - (time.monotonic() - capture["started_monotonic"]), deadline - time.monotonic())
                _need(remaining > 0, "occluder capture deadline exhausted")
                result = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(row["id"]), str(file)],
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=remaining, check=False)
                _need(result.returncode == 0 and file.is_file() and 45 <= file.stat().st_size <= MAX_ALPHA_BYTES - total,
                      "bounded actual per-window alpha capture failed")
                raw = file.read_bytes(); total += len(raw)
                capture["after"] = [self._visibility_row(item) for item in self._inventory(deadline)]
                capture["finished_monotonic"] = time.monotonic()
                capture["png"] = {"base64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                _alpha_evidence(capture, row, rows, bounds, visibility["capture_scale"], window)
                captures.append(capture)
        return captures

    def inspect(self, identity, observer, deadline):
        ax, Q = self.ax, self.ax.Q
        system = ax.context(identity, deadline, read_focus=False)
        _system(system, identity)
        app = ax.application(identity["pid"])
        window = self._get(app, "AXFocusedWindow", identity, deadline)
        scalar = lambda node, key, optional=False: S._scalar(self._get(node, key, identity, deadline, optional=optional), ax.native_scalar)
        _need(scalar(window, "AXMain") is True, "focused window must already be the current main window")
        position = [float(v) for v in ax.vector(self._get(window, "AXPosition", identity, deadline), "position")]
        size = [float(v) for v in ax.vector(self._get(window, "AXSize", identity, deadline), "size")]
        visible = self._inventory(deadline)
        ordinary = [w for w in visible if int(w.get(Q.kCGWindowLayer, -1)) == 0 and bool(w.get(Q.kCGWindowIsOnscreen, False)) and
                    float(w.get(Q.kCGWindowAlpha, 0)) > 0 and float(w.get(Q.kCGWindowBounds, {}).get("Width", 0)) > 80 and
                    float(w.get(Q.kCGWindowBounds, {}).get("Height", 0)) > 80]
        _need(ordinary and int(ordinary[0].get(Q.kCGWindowOwnerPID, -1)) == identity["pid"], "owned video window must already be foremost")
        title, document = scalar(window, "AXTitle"), scalar(window, "AXDocument")
        front = _select_owned_window(ordinary, identity, position, size, title, document, Q)
        selected = {"id": int(front[Q.kCGWindowNumber]), "pid": identity["pid"], "role": scalar(window, "AXRole"),
                    "subrole": scalar(window, "AXSubrole", True), "title": title,
                    "document": document, "position": position, "size": size}
        selected_indices = [i for i, row in enumerate(visible) if int(row.get(Q.kCGWindowNumber, -1)) == selected["id"]]
        _need(len(selected_indices) == 1, "selected current window is absent or duplicated in Quartz inventory")
        visibility = {"selected": self._visibility_row(front), "above": [self._visibility_row(row) for row in visible[:selected_indices[0]]],
                      "below": [self._visibility_row(row) for row in visible[selected_indices[0] + 1:]], "alpha_captures": []}
        displays = ax.displays(deadline)
        _need(len(displays) == 1, "full-display playback capture currently supports exactly one display")
        matching = [d for d in displays if all(d["position"][i] <= position[i] and position[i] + size[i] <= d["position"][i] + d["size"][i] for i in (0, 1))]
        _need(len(matching) == 1, "selected video must fit exactly one current display")
        screens = [screen for screen in ax.K.NSScreen.screens() if int(screen.deviceDescription()["NSScreenNumber"]) == matching[0]["id"]]
        _need(len(screens) == 1, "selected current backing display disappeared")
        scale = float(screens[0].backingScaleFactor())
        _need(_number(scale, 1, 8) and scale.is_integer(), "bounded exact display backing scale required")
        visibility["capture_scale"] = int(scale)
        visibility["alpha_captures"] = self._alpha_captures(visibility, selected, observer["video"], deadline)
        control = app
        for step in observer["control"]["path"]:
            control = self._get(control, step["attribute"], identity, deadline)
            if step["index"] is not None:
                _need(ax.is_array(control) and len(control) <= 128 and step["index"] < len(control), "public AX control relation changed")
                control = control[step["index"]]
        result = {"format": SNAPSHOT, "system": system, "window": selected, "display": matching[0], "displays": displays, "visibility": visibility,
                  "control": {"role": scalar(control, "AXRole"), "identifier": scalar(control, "AXIdentifier", True),
                              "title": scalar(control, "AXTitle"), "enabled": scalar(control, "AXEnabled")}}
        return validate_snapshot(result, observer, identity)

    def frame(self, display, file, deadline):
        Q = self.ax.Q
        _need(Q.CGPreflightScreenCaptureAccess(), "existing Screen Recording permission required; no prompt")
        displays = self.ax.displays(deadline)
        _need(len(displays) == 1, "ambiguous multi-display capture is unsupported")
        # screencapture numbers displays from one in NSScreen order, which is
        # the native enumeration used by S._Mac.displays (not sorted geometry).
        indices = [i + 1 for i, row in enumerate(displays) if row["id"] == display["id"]]
        _need(len(indices) == 1 and time.monotonic() < deadline, "selected capture display disappeared")
        result = subprocess.run(["/usr/sbin/screencapture", "-x", "-D", str(indices[0]), str(file)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=max(.1, min(8, deadline - time.monotonic())), check=False)
        _need(result.returncode == 0 and file.is_file() and file.stat().st_size <= MAX_PNG_BYTES, "actual full-display capture failed")


def current_context(observer, identity, *, deadline):
    R.assert_guest()
    observer, identity = validate_observer(observer), S.validate_identity(identity)
    snapshot = _Mac().inspect(identity, observer, deadline)
    document = snapshot["window"]["document"]
    parsed = urlsplit(document)
    _need(parsed.scheme == "file" and parsed.netloc == "" and not parsed.query and not parsed.fragment, "focused document must be an exact local file URI")
    path = Path(unquote(parsed.path))
    _need(path.resolve(strict=True) == path and path.is_file() and path.as_uri() == document and path.stat().st_size <= MAX_MEDIA_BYTES, "current media file is absent, aliased, or outside the byte budget")
    pin = {"file": str(path), "sha256": R._file_hash(path), "bytes": path.stat().st_size}
    reader = E._Reader(); reader.read(pin, "media", maximum=MAX_MEDIA_BYTES, retain=False)
    return validate_context({"format": CONTEXT, "window": snapshot["window"], "media": pin})
