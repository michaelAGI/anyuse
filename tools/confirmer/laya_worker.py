"""Container-only worker for the confirmer.

Runs INSIDE the Apple container (internal network, no DNS, HF offline flags),
never on the host: AnyUse copies it into the container's /data and starts it
through /data/run.sh. Reads /data/in.jsonl (``{"id", "state", "question"}``),
writes /data/out_<checkpoint>.jsonl (``{"id", "noul", "confidence", "ms"}`` or
``{"id", "error"}``) and /data/meta_<checkpoint>.json (load time, threads).

    python laya_worker.py en typed          # batch over /data/in.jsonl
    python laya_worker.py serve en 600      # warm session: JSON lines on stdin/stdout

In ``serve`` mode nothing is mounted but the laya directory: requests arrive
on stdin, one JSON object per line, answers leave on stdout in the same form;
the first stdout line is ``{"ready": true, ...}``. The worker exits when
stdin closes or after ``idle_s`` seconds without a request.
"""
import json
import os
import socket
import sys
import time


def main(checkpoints):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:                                    # the network must be closed
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        net = "REACHABLE"
    except Exception as exc:
        net = "blocked " + type(exc).__name__
    import torch
    torch.set_num_threads(8)
    from laya import Agent
    reqs = [json.loads(line) for line in open("/data/in.jsonl", encoding="utf-8") if line.strip()]
    for ck in checkpoints:
        if ck not in ("en", "typed"):
            continue
        t0 = time.perf_counter()
        agent = Agent(f"/work/models/{ck}")
        load_s = time.perf_counter() - t0
        if reqs:                            # warm-up, not timed
            agent.system_one(reqs[0]["state"], {"q": reqs[0]["question"]})
        with open(f"/data/out_{ck}.jsonl", "w", encoding="utf-8") as out:
            for r in reqs:
                t1 = time.perf_counter()
                try:
                    a = agent.system_one(r["state"], {"q": r["question"]})["answers"]["q"]
                    rec = {"id": r["id"], "noul": a.get("noul"),
                           "confidence": a.get("confidence"),
                           "ms": round((time.perf_counter() - t1) * 1000.0, 1)}
                except Exception as exc:
                    rec = {"id": r["id"], "error": f"{type(exc).__name__}: {exc}"[:200]}
                out.write(json.dumps(rec) + "\n")
        with open(f"/data/meta_{ck}.json", "w", encoding="utf-8") as fh:
            json.dump({"checkpoint": ck, "load_s": round(load_s, 2), "n": len(reqs),
                       "network": net, "threads": torch.get_num_threads()}, fh)
        del agent
    print("network:", net)


def _answer(agent, r):
    t1 = time.perf_counter()
    try:
        a = agent.system_one(r["state"], {"q": r["question"]})["answers"]["q"]
        return {"id": r.get("id"), "noul": a.get("noul"), "confidence": a.get("confidence"),
                "ms": round((time.perf_counter() - t1) * 1000.0, 1)}
    except Exception as exc:
        return {"id": r.get("id"), "error": f"{type(exc).__name__}: {exc}"[:200]}


def serve(checkpoint, idle_s):
    import select
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        net = "REACHABLE"
    except Exception as exc:
        net = "blocked " + type(exc).__name__
    import torch
    torch.set_num_threads(8)
    from laya import Agent
    t0 = time.perf_counter()
    agent = Agent(f"/work/models/{checkpoint}")
    load_s = time.perf_counter() - t0
    agent.system_one("warm-up page", {"q": {"type": "noul", "instructions": "Is this a page?"}})
    out = sys.stdout
    out.write(json.dumps({"ready": True, "checkpoint": checkpoint, "load_s": round(load_s, 2),
                          "network": net}) + "\n")
    out.flush()
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], idle_s)
        if not ready:
            break                               # idle timeout
        line = sys.stdin.readline()
        if not line:
            break                               # stdin closed
        try:
            r = json.loads(line)
        except ValueError:
            continue
        out.write(json.dumps(_answer(agent, r)) + "\n")
        out.flush()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["serve"]:
        serve(args[1] if len(args) > 1 else "en", float(args[2]) if len(args) > 2 else 600.0)
    else:
        main(args or ["en"])
