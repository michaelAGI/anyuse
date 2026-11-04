"""Fixed Ghidra extraction of original code and imported Qt pointer records.

The model supplies bundle-relative image names and bounded offsets only.
Ghidra imports a private copy; this never executes or attaches to the app.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from . import rea_mapping as R

RESERVE_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 64 * 1024**2
MAX_OUTPUT_BYTES = 8 * 1024**2
SCRIPT_NAME = "AnyUseStaticEvidence"
SCRIPT = r'''
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.framework.Application;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.reloc.Relocation;
import com.google.gson.Gson;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class AnyUseStaticEvidence extends GhidraScript {
    private Map<String,Object> object(Object... pairs) {
        Map<String,Object> result = new LinkedHashMap<>();
        for (int i=0; i<pairs.length; i+=2) result.put((String)pairs[i], pairs[i+1]);
        return result;
    }
    private String hex(byte[] raw) {
        StringBuilder s = new StringBuilder();
        for (byte b: raw) s.append(String.format("%02x", b & 255));
        return s.toString();
    }
    private String offset(Address at) { return "0x" + Long.toHexString(at.subtract(currentProgram.getImageBase())); }
    private String address(Address at) { return "0x" + at.toString().replaceFirst("^0+(?!$)", ""); }
    private Address at(String value) { return currentProgram.getImageBase().add(Long.parseUnsignedLong(value.substring(2),16)); }
    private byte[] bytes(Address at, int count) throws Exception {
        if (count<1 || count>16384) throw new Exception("bounded original range required");
        byte[] value = new byte[count];
        if (currentProgram.getMemory().getBytes(at,value)!=count) throw new Exception("incomplete imported range");
        return value;
    }
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length!=4) throw new Exception("output, entry offsets, record offsets and data ranges required");
        Map<String,Object> result = object("ghidra",Application.getApplicationVersion(),
            "language",currentProgram.getLanguageID().toString(), "image_base",address(currentProgram.getImageBase()),
            "big_endian",currentProgram.getLanguage().isBigEndian());
        List<Object> sites = new ArrayList<>(), records = new ArrayList<>(), data = new ArrayList<>();
        result.put("sites",sites); result.put("records",records); result.put("data_ranges",data);
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram)) throw new Exception("decompiler could not open this program");
            for (String entry: args[1].split(",")) {
                Address start = at(entry);
                Function function = currentProgram.getFunctionManager().getFunctionAt(start);
                boolean seeded = function == null;
                if (seeded) {
                    disassemble(start);
                    function = createFunction(start, "anyuse_observed_"+entry.substring(2));
                }
                if (function==null) throw new Exception("no function at declared code offset "+entry);
                DecompileResults decompiled = decompiler.decompileFunction(function,30,monitor);
                if (!decompiled.decompileCompleted() || decompiled.getDecompiledFunction()==null)
                    throw new Exception("decompilation incomplete at "+entry+": "+decompiled.getErrorMessage());
                List<Object> ranges = new ArrayList<>(), instructions = new ArrayList<>();
                AddressRangeIterator iter = function.getBody().getAddressRanges(true);
                int total = 0;
                while (iter.hasNext()) {
                    AddressRange range = iter.next();
                    long length = range.getLength();
                    if (length>16384 || ranges.size()>=128 || total+length>131072)
                        throw new Exception("function body exceeds extraction bound");
                    byte[] raw = bytes(range.getMinAddress(),(int)length); total+=raw.length;
                    ranges.add(object("image_offset",offset(range.getMinAddress()),"bytes",raw.length,"imported_hex",hex(raw)));
                }
                InstructionIterator listing = currentProgram.getListing().getInstructions(function.getBody(),true);
                while (listing.hasNext()) {
                    Instruction instruction = listing.next();
                    if (instructions.size()>=4096 || instruction.getLength()!=4)
                        throw new Exception("unsupported or oversized instruction listing");
                    instructions.add(object("address",address(instruction.getAddress()),"image_offset",offset(instruction.getAddress()),
                        "bytes",hex(instruction.getBytes()),"mnemonic",instruction.getMnemonicString(),
                        "text",instruction.toString(),"flow",instruction.getFlowType().toString()));
                }
                String body = decompiled.getDecompiledFunction().getC();
                sites.add(object("image_offset",entry,"status","decompiled_static_code_observation",
                    "decompile_completed",true,"cfg_complete_claimed",false,"analysis_entry_seeded",seeded,
                    "body_imported_ranges",ranges,"instructions",instructions,
                    "decompiled_text",body.substring(0,Math.min(body.length(),131072)),
                    "decompiled_text_truncated",body.length()>131072));
            }
            if (!args[2].equals("none")) for (String record: args[2].split(",")) {
                Address start = at(record);
                List<Object> pointers = new ArrayList<>();
                for (int index: new int[]{1,2,3}) {
                    Address slot = start.add(8*index);
                    List<Relocation> relocations = currentProgram.getRelocationTable().getRelocations(slot);
                    if (relocations.size()!=1) throw new Exception("original pointer needs one exact loader relocation");
                    Relocation relocation = relocations.get(0);
                    long target = currentProgram.getMemory().getLong(slot);
                    Address to = currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(target);
                    pointers.add(object("slot",address(slot),"target",address(to),"target_image_offset",offset(to),
                        "relocation_status",relocation.getStatus().toString(),"relocation_type",relocation.getType(),
                        "imported_hex",hex(bytes(slot,8))));
                }
                records.add(object("image_offset",record,"imported_hex",hex(bytes(start,56)),"pointers",pointers));
            }
            if (!args[3].equals("none")) for (String range: args[3].split(",")) {
                String[] parts = range.split(":");
                if (parts.length!=2 || data.size()>=256) throw new Exception("bounded data ranges required");
                int count=Integer.parseInt(parts[1]);
                data.add(object("image_offset",parts[0],"bytes",count,"imported_hex",hex(bytes(at(parts[0]),count))));
            }
            Files.writeString(Path.of(args[0]),new Gson().toJson(result),StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW,StandardOpenOption.WRITE);
        } finally { decompiler.dispose(); }
    }
}
'''


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _run(argv, folder, deadline):
    env = dict(os.environ)
    # Keep tool preferences, compiler caches and temporary files inside this
    # analysis, while retaining the explicitly configured Java/toolchain.
    _need(not any(char in str(folder) for char in ('"','\\','\n','\r')),
          "analysis output path cannot contain Java-option quoting characters")
    private_home, private_tmp = folder / "tool-home", folder / "tmp"
    private_home.mkdir(mode=0o700); private_tmp.mkdir(mode=0o700)
    env.update(HOME=str(private_home), TMPDIR=str(private_tmp),
               XDG_CONFIG_HOME=str(private_home / ".config"), XDG_CACHE_HOME=str(private_home / ".cache"),
               XDG_DATA_HOME=str(private_home / ".local/share"))
    env.pop("CFFIXED_USER_HOME",None)
    env["JAVA_TOOL_OPTIONS"] = '-Xmx2048m -XX:MaxMetaspaceSize=512m -Djava.awt.headless=true "-Duser.home='+str(private_home)+'"'
    with (folder / "headless.stdout").open("xb") as out, (folder / "headless.stderr").open("xb") as err:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                 cwd=folder, env=env, start_new_session=True)
        try:
            while child.poll() is None:
                _need(time.monotonic() < deadline, "fixed Ghidra extraction deadline expired")
                _need(shutil.disk_usage(folder).free >= RESERVE_BYTES, "Ghidra extraction reached the storage reserve")
                _need(sum(p.stat().st_size for p in (folder / "headless.stdout", folder / "headless.stderr")) <= MAX_OUTPUT_BYTES,
                      "Ghidra diagnostics exceeded their byte bound")
                time.sleep(.25)
            _need(child.returncode == 0, "fixed Ghidra extraction failed; inspect retained diagnostics")
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)


def build(bundle, request, out, *, timeout=180):
    from . import rea_static_artifacts as A
    R.assert_guest()
    _need(type(timeout) is int and 1 <= timeout <= 600, "bounded Ghidra evidence timeout required")
    folder = Path(out)
    _need(not folder.exists(), "new Ghidra evidence output required")
    home = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/missing-ghidra"))
    launcher = home / "support/analyzeHeadless"
    if not launcher.is_file():
        folder.mkdir(mode=0o700)
        value = {"format": "anyuse.rea-missing-static-capability/1", "status": "needs_capability",
                 "reason": "configured_ghidra_unavailable", "required": "GHIDRA_INSTALL_DIR/support/analyzeHeadless"}
        R.write_json(folder / "evidence.json", value)
        return {**value, "receipt": str(folder / "evidence.json"), "proof_created": False, "evidence_only": True}
    image = A.OriginalImage(bundle, request["relative"])
    try:
        _need(image.pin["bytes"] <= MAX_IMAGE_BYTES, "original image exceeds fixed extraction working-space bound")
        _need(shutil.disk_usage(folder.parent).free >= RESERVE_BYTES + 4 * image.pin["bytes"] + 128 * 1024**2,
              "insufficient working space above the original storage reserve")
        for offset in request["entry_offsets"]:
            image.read(int(offset,16),4,kind="code")
        for offset in request["record_offsets"]:
            image.read(int(offset,16),56)
        data_ranges = request.get("data_ranges", [])
        for row in data_ranges:
            image.read(int(row["offset"],16),row["bytes"])
        folder.mkdir(mode=0o700)
        private = folder / "original-image.dylib"
        script = folder / (SCRIPT_NAME + ".java")
        with script.open("x") as stream:
            stream.write(SCRIPT)
        raw_path = folder / "ghidra-observation.json"
        argv = [str(launcher), str(folder), "analysis", "-import", str(private), "-deleteProject",
                "-processor", "AARCH64:LE:64:AppleSilicon", "-analysisTimeoutPerFile", str(max(1,timeout-30)),
                "-scriptPath", str(folder), "-postScript", script.name, str(raw_path),
                ",".join(request["entry_offsets"]), ",".join(request["record_offsets"]) or "none",
                ",".join(row["offset"]+":"+str(row["bytes"]) for row in data_ranges) or "none"]
        from . import rea_analysis_cache as cache
        ghidra = cache.ghidra_pin()
        cache_key = (cache.key({"format": "anyuse.rea-ghidra-code-at-offsets/1", "source_sha256": image.pin["sha256"],
                               "request": request, "script_sha256": R._file_hash(script), "ghidra": ghidra})
                     if ghidra is not None else None)
        store = cache.StaticAnalysisCache()
        observed = store.get(cache_key) if cache_key is not None else None
        cache_hit = type(observed) is dict
        launcher_sha256 = R._file_hash(launcher)
        R.write_json(folder / "request.json", {"argv": argv, "original": image.pin,
            "script_sha256": R._file_hash(script), "script_is_fixed": True, "app_execution": False,
            "launcher_sha256": launcher_sha256, "timeout_seconds": timeout,
            "cache": {"hit": cache_hit, "key": cache_key, "evidence_only": True}})
        if cache_hit:
            R.write_json(raw_path, observed)
        else:
            with image.path.open("rb") as source, private.open("xb") as output:
                shutil.copyfileobj(source,output,1024**2)
            _need(R._file_hash(private) == image.pin["sha256"], "private analysis image differs from its original")
            _run(argv, folder, time.monotonic()+timeout)
        _need(raw_path.is_file() and 0 < raw_path.stat().st_size <= MAX_OUTPUT_BYTES, "bounded Ghidra observation required")
        observed = R.load_json(raw_path)
        _need(observed["ghidra"] == "12.1.4" and observed["language"] == "AARCH64:LE:64:AppleSilicon" and
              observed["big_endian"] is False and observed["image_base"] == hex(image.base), "unsupported Ghidra image interpretation")
        _need([s["image_offset"] for s in observed["sites"]] == request["entry_offsets"] and
              [s["image_offset"] for s in observed["records"]] == request["record_offsets"], "Ghidra evidence offsets differ")
        _need([{ "offset": row["image_offset"], "bytes": row["bytes"]} for row in observed["data_ranges"]] == data_ranges,
              "Ghidra imported data ranges differ from their request")
        for row in observed["data_ranges"]:
            raw = image.read(int(row["image_offset"],16),row["bytes"])
            if row["image_offset"] not in request["record_offsets"]:
                _need(row["imported_hex"] == raw.hex(), "non-record metadata was relocated or changed")
        sites = []
        for site in observed["sites"]:
            body = []
            for row in site["body_imported_ranges"]:
                raw = image.read(int(row["image_offset"],16),row["bytes"],kind="code")
                _need(raw.hex() == row["imported_hex"], "Ghidra instruction bytes differ from the unchanged original")
                body.append({"image_offset": row["image_offset"], "bytes": len(raw), "original_hex": raw.hex(),
                             "sha256": hashlib.sha256(raw).hexdigest(), "original_equals_imported": True})
            sites.append({k:v for k,v in site.items() if k!="body_imported_ranges"} | {"body_original_ranges":body})
        image.unchanged()
        _need(image._hash() == image.pin["sha256"], "original image changed during Ghidra analysis")
        _need(R._file_hash(launcher) == launcher_sha256 and (ghidra is None or cache.ghidra_pin() == ghidra),
              "Ghidra toolchain changed during analysis")
        value = {"format": "anyuse.rea-ghidra-code-at-offsets/1", "status": "completed_static_code_observation",
                 "original_file_unchanged": True, "automatic_abi_or_lifetime_admission": False, "cfg_complete_claimed": False,
                 "task_proof": False, "ghidra": observed["ghidra"], "language": observed["language"],
                 "image": image.pin, "sites": sites, "records": observed["records"], "data_ranges": observed["data_ranges"]}
        R.write_json(folder / "evidence.json", value)
        if cache_key is not None and not cache_hit:
            store.put(cache_key, observed)
        return {"status": value["status"], "receipt": str(folder / "evidence.json"), "proof_created": False,
                "evidence_only": True, "app_execution": False, "automatic_abi_or_lifetime_admission": False,
                "cache": {"hit": cache_hit, "key": cache_key}}
    finally:
        image.close()
