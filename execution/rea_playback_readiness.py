"""Bounded read-only public pixel readiness for ordinary playback setup.

This preparation receipt never replaces the unchanged temporal proof consumer.
Only actual black/still pixels permit another guarded sample. All uncertainty
and budget failures remain terminal; no app action or native actor is retried.
"""
from pathlib import Path
import hashlib
import json
import os
import time

from . import rea_mapping as R
from . import rea_ax_state as S
from . import rea_state_evidence as E
from . import rea_playback_state as O
from .rea_playback_state import (validate_observer, validate_context, validate_snapshot,
    validate_observation, image_metrics, temporal_metrics, _number, _need,
    _alpha_interval, _stable_snapshot, MAX_PNG_BYTES, MAX_MEDIA_BYTES, OBSERVATION)

READINESS = "anyuse.rea-public-playback-readiness/1"
MAX_READINESS_FRAMES, MAX_READINESS_PNG_BYTES = 9, 64 * 1024**2

def wait_for_playing(observer, target, identity, context, out, *, deadline):
    """Read-only public setup readiness; never a trial observation or task proof.

    Only measured black or insufficiently moving pixels permit another sample.
    Any changed ownership/context, visibility, control or capture error refuses.
    The caller still takes its own fresh complete before/after proof windows.
    """
    R.assert_guest()
    from . import foreground_interlock as F
    return _wait_for_playing(observer, target, identity, context, out, deadline=deadline,
        backend=O._Mac(), clock=time.monotonic, sleep=time.sleep,
        check=lambda: F.require_clear("campaign.public-pixel-readiness"))


def _readiness_space(path, remaining_png_bytes, remaining_frame_json_bytes, *, inflight_png_bytes=MAX_PNG_BYTES):
    # Admit retained PNGs/individual frame metadata, one in-flight maximum PNG,
    # two atomic manifest copies and free headroom. This is not an OS quota.
    stats = os.statvfs(path)
    available = stats.f_bavail * stats.f_frsize
    required = remaining_png_bytes + remaining_frame_json_bytes + inflight_png_bytes + 2 * R.MAX_JSON_BYTES + 16 * 1024**2
    _need(available >= required, "public readiness storage reserve unavailable")
    return {"available_bytes": available, "required_bytes": required}


def _wait_for_playing(observer, target, identity, context, out, *, deadline, backend, clock, sleep, check):
    observer, target, identity, context = validate_observer(observer), S.validate_target(target), S.validate_identity(identity), validate_context(context)
    _need(_number(deadline) and deadline > clock(), "bounded public readiness deadline required")
    folder = Path(out)
    _need(folder.is_absolute() and folder.parent.resolve(strict=True) == folder.parent and
          not folder.exists() and not folder.is_symlink(), "fresh canonical public readiness output required")
    frame_json_budget = MAX_READINESS_FRAMES * R.MAX_JSON_BYTES
    space = _readiness_space(folder.parent, MAX_READINESS_PNG_BYTES, frame_json_budget)
    folder.mkdir(mode=0o700)
    receipt = folder / "readiness.json"
    value = {"format": READINESS, "status": "sampling", "target": target, "identity": identity, "context": context,
        "started_monotonic": clock(), "finished_monotonic": None, "deadline_monotonic": deadline,
        "limits": {"frames": MAX_READINESS_FRAMES, "png_bytes": MAX_READINESS_PNG_BYTES,
                   "frame_json_bytes": R.MAX_JSON_BYTES, "aggregate_frame_json_bytes": frame_json_budget,
                   "receipt_bytes": R.MAX_JSON_BYTES},
        "storage": [space], "samples": [], "decisions": [], "read_only": True,
        "native_goal_calls": 0, "model_calls": 0, "proof_created": False, "error": None}
    images, window, previous, common, retained_bytes, metadata_bytes = [], [], None, None, 0, 0
    def retain():
        # Final refusal remains small even when future frame admission failed.
        value["storage"].append(_readiness_space(folder, 0, 0, inflight_png_bytes=0))
        raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
        _need(len(raw) <= R.MAX_JSON_BYTES, "public readiness receipt byte bound exhausted")
        R.write_json(receipt, value)
    try:
        for index in range(MAX_READINESS_FRAMES):
            if previous is not None:
                delay = max(0, previous + observer["video"]["interval_seconds"] - clock())
                _need(clock() + delay < deadline, "public readiness deadline exhausted")
                sleep(delay)
            _need(clock() < deadline, "public readiness deadline exhausted")
            check()
            value["storage"].append(_readiness_space(folder, MAX_READINESS_PNG_BYTES - retained_bytes, frame_json_budget - metadata_bytes))
            sample = {"started_monotonic": clock(), "before": backend.inspect(identity, observer, deadline)}
            before = validate_snapshot(sample["before"], observer, identity, context)
            _need(before["control"]["title"] == observer["control"]["playing_title"], "public control stopped playing during readiness")
            file = folder / f"frame-{index:02d}-display.png"
            sample["capture_started_monotonic"] = previous = clock()
            backend.frame(before["display"], file, deadline)
            sample["capture_finished_monotonic"] = clock()
            sample["after"] = backend.inspect(identity, observer, deadline)
            sample["finished_monotonic"] = clock()
            moments = [sample[key] for key in ("started_monotonic", "capture_started_monotonic", "capture_finished_monotonic", "finished_monotonic")]
            _need(all(_number(moment) for moment in moments) and moments[0] <= moments[1] < moments[2] <= moments[3] <= deadline,
                  "public readiness capture ordering or deadline changed")
            after = validate_snapshot(sample["after"], observer, identity, context)
            check()
            _alpha_interval(before, sample["started_monotonic"], sample["capture_started_monotonic"])
            _alpha_interval(after, sample["capture_finished_monotonic"], sample["finished_monotonic"])
            _need(S._same(_stable_snapshot(before, observer["video"]), _stable_snapshot(after, observer["video"])) and
                  (common is None or S._same(_stable_snapshot(before, observer["video"]), _stable_snapshot(common, observer["video"]))),
                  "public readiness process/window/display/control changed")
            _need(sample["finished_monotonic"] <= deadline and file.is_file() and not file.is_symlink() and
                  file.stat().st_size <= min(MAX_PNG_BYTES, MAX_READINESS_PNG_BYTES - retained_bytes),
                  "public readiness deadline or PNG byte bound exhausted")
            os.chmod(file, 0o600)
            raw = file.read_bytes(); retained_bytes += len(raw)
            sample["image"] = {"file": str(file), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            sample["metrics"], pixels = image_metrics(raw, before["window"], before["display"], observer["video"])
            _need(all(abs(sample["metrics"]["display_size"][axis] - before["display"]["size"][axis] * before["visibility"]["capture_scale"]) < 1e-6
                      for axis in (0, 1)), "readiness capture differs from observed backing scale")
            count = observer["video"]["frames"]
            # Preserve each full CG/alpha bookend once, outside the small
            # manifest; real display metadata can be megabytes per frame.
            frame_raw = (json.dumps(sample, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
            _need(len(frame_raw) <= R.MAX_JSON_BYTES and metadata_bytes + len(frame_raw) <= frame_json_budget,
                  "public readiness frame metadata byte bound exhausted")
            frame_file = folder / f"frame-{index:02d}.json"
            _need(not frame_file.exists() and not frame_file.is_symlink(), "fresh readiness frame metadata required")
            R.write_json(frame_file, sample)
            _need(frame_file.stat().st_size == len(frame_raw) and R._file_hash(frame_file) == hashlib.sha256(frame_raw).hexdigest(),
                  "public readiness frame metadata changed while retaining")
            metadata_bytes += len(frame_raw)
            value["samples"].append({"index": index, "frame": {"file": str(frame_file), "bytes": len(frame_raw), "sha256": hashlib.sha256(frame_raw).hexdigest()},
                                     "image": sample["image"], "metrics": sample["metrics"]})
            images = (images + [pixels])[-count:]; window = (window + [sample])[-count:]; common = before
            nonblack = all(row["metrics"]["nonblack_fraction"] >= observer["video"]["min_nonblack_fraction"] for row in window)
            deltas = temporal_metrics(images[-count:]) if len(window) == count else []
            moving = len(window) == count and all(delta >= observer["video"]["motion_min"] for delta in deltas)
            status = "black" if not nonblack else "collecting" if len(window) < count else "ready" if moving else "not_moving"
            value["decisions"].append({"sample": index, "status": status, "window_samples": len(window), "deltas": deltas})
            if status == "ready":
                # Reopen every final frame through the unchanged strict pixel
                # consumer. Setup's receipt never substitutes for trial proof.
                candidate = {"format": OBSERVATION, "target": target, "identity": identity, "context": context,
                    "state": "playing", "started_monotonic": window[0]["started_monotonic"],
                    "finished_monotonic": sample["finished_monotonic"], "samples": window, "deltas": deltas,
                    "read_only": True, "proof_created": False}
                reader = E._Reader()
                for summary, actual in zip(value["samples"][-count:], window):
                    retained = reader.json(summary["frame"], "public_readiness_frame", maximum=R.MAX_JSON_BYTES)
                    _need(S._same(retained, actual), "retained readiness frame differs from actual sample")
                reader.read(context["media"], "public_readiness_media", maximum=MAX_MEDIA_BYTES, retain=False)
                validate_observation(candidate, observer, target, identity, context, "playing", reader); reader.recheck()
                check()
                value.update(status="ready", finished_monotonic=clock())
                _need(value["finished_monotonic"] < deadline, "public readiness completed after deadline")
                retain()
                return {"format": READINESS, "status": "ready", "receipt": {"file": str(receipt), "bytes": receipt.stat().st_size, "sha256": R._file_hash(receipt)},
                    "sample_count": len(value["samples"]), "read_only": True, "native_goal_calls": 0, "model_calls": 0, "proof_created": False}
            retain()
        raise ValueError("public video did not become nonblack and moving within bounded setup sampling")
    except BaseException as error:
        value.update(status="refused", finished_monotonic=clock(), error={"type": type(error).__name__, "message": str(error)[:2048]})
        # Retaining diagnostics never turns a refused sample into readiness.
        try:
            retain()
        except BaseException as retention_error:
            if hasattr(error, "add_note"):
                error.add_note("Readiness diagnostics could not be retained: " + type(retention_error).__name__)
        raise


