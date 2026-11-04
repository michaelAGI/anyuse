"""Offline inert JS checks: no Frida, native execution, compiler or lifetime proof."""
import base64
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_native_path as P
from anyuse.execution import rea_native_receiver_contract as NX
from anyuse.execution import rea_runtime_bridge as B
from test_rea_native_receiver_contract import declared as actual_receiver_declaration, no_live_runtime


MAIN = "/fixture/Path.app/Contents/MacOS/Path"
UUID = "0123456789abcdef0123456789abcdef"


def _case(*, root_kind="getter", paths=1, later_getter=True, identity=False, image_path=MAIN,
          data_mode=None, data_offset=None):
    module = {"path": image_path, "sha256": "a" * 64}
    abi = {"kind": "header", "file": "/fixture/abi.json", "sha256": "b" * 64, "pointer": "/root"}
    getter = {"kind": "native_function", "target": {"module": copy.deepcopy(module), "symbol": "root"},
              "abi": "default", "arguments": [], "return_type": "pointer", "signature_evidence": abi}
    receiver = {"class": "Controller", "kind": "unique", "subclasses": False}
    root = {"getter": getter}
    if root_kind == "objc":
        root = {"objc": receiver}
    elif root_kind == "read":
        root = {"read": {"receiver": receiver, "receiver_class": "Controller", "module": copy.deepcopy(module),
                         "field": {"class": "Controller", "name": "owner", "encoding": "^v", "offset": 8},
                         "evidence": copy.deepcopy(abi)}}
    if data_mode is not None:
        root_kind = "data"
        data_offset = data_offset or ("0x620" if data_mode == "object" else "0x650")
        root = {"data": {"module": copy.deepcopy(module), "offset": data_offset, "mode": data_mode}}

    def evidence(pointer):
        return {"claim": {"kind": "reviewed_contract", "file": "/fixture/path.json",
                          "sha256": "c" * 64, "pointer": pointer},
                "originals": [{"role": "header", "file": "/fixture/objects.h", "sha256": "d" * 64},
                              {"role": "independent_review", "file": "/fixture/review.json", "sha256": "e" * 64}]}

    def layout(name, fields):
        return {"format": P.LAYOUT, "name": name, "owner_image": copy.deepcopy(module), "abi": dict(P.ABI),
                "bytes": 16, "alignment": 8, "fields": fields, "evidence": evidence("/" + name)}

    argument_types = ["pointer"] * (paths + int(later_getter)) + ["int32"]
    call = {"module": copy.deepcopy(module), "symbol": "consume", "abi": "default",
            "argument_types": argument_types, "return_type": "int32", "context": "none",
            "pointer_policy": "borrowed_no_escape", "signature_evidence": copy.deepcopy(abi)}
    recipes = []
    for slot in range(paths):
        recipe = {"format": P.FORMAT, "root": copy.deepcopy(root), "root_type": "Owner", "result_type": "Node",
                  "types": [layout("Owner", [{"name": "ready", "type": "bool", "offset": 0},
                                               {"name": "child", "type": "pointer", "offset": 8, "target": "Node"}]),
                            layout("Node", [{"name": "generation", "type": "uint32", "offset": 0},
                                              {"name": "payload", "type": "int32", "offset": 4}])],
                  "path": ["child"], "preconditions": [{"hop": 0, "field": "ready", "equals": True},
                                                           {"hop": 1, "field": "generation", "equals": 7}],
                  "root_evidence": evidence("/root"),
                  "lifetime": {"mode": "process_lifetime" if root_kind in ("getter", "data") else "retained_owner_main_queue_stable",
                               "evidence": evidence("/lifetime")},
                  "consumer": {"call": copy.deepcopy(call), "argument": slot, "pointee_type": "Node",
                               "role": "cxx_this" if slot == 0 else "borrowed_pointer", "dispatch": "direct",
                               "this_adjustment": 0, "evidence": evidence("/consumer" + str(slot))}}
        if identity:
            recipe["types"][1]["fields"].append({"name": "vptr", "type": "pointer", "offset": 8})
            recipe["types"][1]["runtime_identity"] = {"kind": "pinned_data_vptr", "field": "vptr",
                "image": copy.deepcopy(module), "data_offset": "0x600"}
        recipes.append(recipe)
    arguments = [{"type": "pointer", "value": {"native_path": recipe}} for recipe in recipes]
    if later_getter:
        mutate = copy.deepcopy(getter)
        mutate["target"]["symbol"] = "mutate"
        arguments.append({"type": "pointer", "value": {"getter": mutate}})
    arguments.append({"type": "int32", "value": 23})
    actor = {"kind": "native_function", "target": {"module": module, "symbol": "consume"}, "abi": "default",
             "arguments": arguments, "return_type": "int32", "signature_evidence": copy.deepcopy(abi)}
    B.validate_actor(actor, allow_bindings=False)
    operation = {key: value for key, value in actor.items() if key != "kind"}
    operation["op"] = "native_invoke"
    image_identity = {"cpu_type": 0x0100000C, "cpu_subtype": 0, "uuid": UUID}
    pins = {image_path: {"file": image_path, "sha256": "a" * 64, "bytes": 4096, "macho_images": [image_identity],
                   "macho_sections": [{"image_identity": image_identity, "sections": [
                       {"segment": "__TEXT", "name": "__text", "offset": "0x400", "bytes": 0x200, "flags": 0x80000400}]}],
                   "macho_segments": [{"image_identity": image_identity, "segments": [
                       {"segment": "__TEXT", "offset": "0x0", "bytes": 4096, "file_offset": 0, "file_bytes": 4096,
                        "max_protection": 5, "initial_protection": 5}]}],
                   "native_path_data_sections": [{"image_identity": image_identity, "sections": [
                       {"segment": "__TEXT", "name": "__const", "offset": "0x600", "bytes": 0x100, "flags": 0}]}]},
            "/fixture/abi.json": {"file": "/fixture/abi.json", "sha256": "b" * 64, "bytes": 100}}
    for recipe in recipes:
        for row in P.evidence_plan(recipe):
            claim = row["claim"]
            pin = pins.setdefault(claim["file"], {"file": claim["file"], "sha256": claim["sha256"], "bytes": 100,
                                                  "native_path_contracts": []})
            record = {"pointer": claim["pointer"], "contract": row["contract"], "originals": row["originals"]}
            if record not in pin["native_path_contracts"]:
                pin["native_path_contracts"].append(record)
            for original in row["originals"]:
                pins[original["file"]] = {"file": original["file"], "sha256": original["sha256"], "bytes": 64}
    return {"operations": [operation], "resources": pins, "root_kind": root_kind, "paths": paths,
            "claimed_module": image_path, "data_mode": data_mode, "data_offset": data_offset}, recipes


def _refresh_contract_pins(case, recipes):
    # Keep adversarial runtime inputs correlated with their exact declarations;
    # a schema/location refusal must not accidentally be a stale evidence refusal.
    records = case["resources"]["/fixture/path.json"]["native_path_contracts"] = []
    for recipe in recipes:
        for row in P.evidence_plan(recipe):
            record = {"pointer": row["claim"]["pointer"], "contract": row["contract"], "originals": row["originals"]}
            if record not in records:
                records.append(record)


def _primary_vptr_case():
    case, recipes = _case(identity=True)
    # An admitted prefix identity with derived state beyond that prefix. This
    # remains an inert layout, not recovered evidence for a real C++ class.
    for recipe in recipes:
        node = recipe["types"][1]
        for field in node["fields"]:
            field["offset"] = {"vptr": 0, "generation": 8, "payload": 12}[field["name"]]
    case["primary_vptr"] = True
    _refresh_contract_pins(case, recipes)
    B.validate_request({"operations": case["operations"]})
    return case, recipes


def _borrowed_root_case(*, read=False, scalar_fields=None):
    case, recipes = _case(later_getter=False, identity=True)
    recipe = recipes[0]
    layout = recipe["types"][1]
    layout["fields"] = [{"name": "vptr", "type": "pointer", "offset": 0},
                        {"name": "generation", "type": "uint32", "offset": 8},
                        {"name": "payload", "type": "int32", "offset": 12}]
    recipe.update(root_type="Node", result_type="Node", types=[layout], path=[],
                  preconditions=[{"hop": 0, "field": "generation", "equals": 7}])
    recipe["lifetime"]["mode"] = "main_queue_borrowed_root"
    case["borrowed_root"] = True
    if read:
        recipe.pop("consumer")
        selected = {"generation", "payload"} if scalar_fields is None else set(scalar_fields)
        fields = [{key: copy.deepcopy(field[key]) for key in ("name", "type", "offset")}
                  for field in layout["fields"] if field["name"] in selected]
        case["operations"] = [{"op": "native_read", "receiver": {"native_path": recipe}, "bytes": layout["bytes"],
                               "fields": fields, "layout_evidence": copy.deepcopy(recipe["root"]["getter"]["signature_evidence"])}]
    _refresh_contract_pins(case, recipes)
    B.validate_request({"operations": case["operations"]})
    return case, recipes


def _receiver_hypothesis_case(*, return_type="int32"):
    # Synthetic registered-observation pins exercise the fixed adapter only;
    # they make no claim about source recovery or a real receiver's lifetime.
    case, _ = _case(later_getter=False)
    module = copy.deepcopy(case["operations"][0]["target"]["module"])
    getter = {"kind": "native_function", "target": {"module": copy.deepcopy(module), "offset": "0x400"},
              "abi": "default", "arguments": [], "return_type": "pointer"}
    consumer = {"kind": "native_function", "target": {"module": copy.deepcopy(module), "offset": "0x440"},
                "abi": "default", "arguments": [{"type": "pointer", "value": {"receiver": "fresh"}},
                                               {"type": "int32", "value": 23}], "return_type": return_type}
    originals = [{"role": role, "file": "/fixture/" + role + ".json", "sha256": digit * 64}
                 for role, digit in zip(("getter_code", "consumer_code", "vptr_data"), "def")]
    request = {"getter": getter, "consumer": consumer,
               "vptr": {"image": module, "data_offset": "0x600"}, "citations": []}
    recipe, actor = NX._decorate(request, "/fixture/path.json", "c" * 64, originals)
    case["operations"] = [{"op": "native_invoke", **{key: value for key, value in actor.items() if key != "kind"}}]
    case["borrowed_root"] = case["receiver_hypothesis"] = True
    for original in originals:
        case["resources"][original["file"]] = {"file": original["file"], "sha256": original["sha256"], "bytes": 100}
    _refresh_contract_pins(case, [recipe])
    case["resources"]["/fixture/path.json"]["native_receiver_signatures"] = [
        {"pointer": "/signatures/getter", "signature": NX.signature_contract(getter)},
        {"pointer": "/signatures/consumer", "signature": NX.signature_contract(consumer)}]
    B.validate_request({"operations": case["operations"]})
    return case, [recipe]


def _observed_root_case(*, read=False, aliases=False, event_limit=64, generation_limit=8, observe_ms=1000):
    case, recipes = _borrowed_root_case(read=read)
    recipe = recipes[0]
    module = copy.deepcopy(recipe["types"][0]["owner_image"])
    def entry(offset): return {"module": copy.deepcopy(module), "offset": offset}
    evidence = copy.deepcopy(recipe["root_evidence"])
    evidence["claim"]["pointer"] = "/observer"
    destructors = [{"entry": entry("0x540"), "this_argument": 0, "this_adjustment": 0}]
    if aliases:
        destructors.append({"entry": entry("0x560"), "this_argument": 0, "this_adjustment": 0})
    observer = {"format": "anyuse.rea-native-lifecycle-root/1",
        "constructor": {"entry": entry("0x500"), "return_sites": [entry("0x580")], "this_argument": 0,
                        "this_adjustment": 0, "completion": "complete_object_normal_return"},
        "destructors": destructors, "owner_thread": "observed_constructor_on_macos_main",
        "selection": "sole_live_at_observation_end", "observe_ms": observe_ms,
        "event_limit": event_limit, "generation_limit": generation_limit, "evidence": evidence}
    recipe["root"] = {"lifecycle": observer}
    recipe["lifetime"]["mode"] = "main_queue_observed_root"
    case["lifecycle_root"] = True
    _refresh_contract_pins(case, recipes)
    B.validate_request({"operations": case["operations"]})
    return case, recipes


_FIXTURE = r"""
const fs=require('fs'),vm=require('vm'),crypto=require('crypto');
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const main=data.main_path??'/fixture/Path.app/Contents/MacOS/Path';
const claimed=data.claimed_module??main,codeBase=data.image_base??(claimed===main?0x1000:0x2000);
const memory=new Map([[0x1000,Buffer.alloc(4096)],[0x3000,Buffer.alloc(16)],[0x4000,Buffer.alloc(16)],
 [0x5000,Buffer.alloc(16)],[0x6000,Buffer.alloc(16)],[0x7000,Buffer.alloc(4096)]]);
const memoryTrace=[];
const borrowedTrace=[];
const header=memory.get(0x1000);
[0xfeedfacf,0x0100000c,0,6,2,256,0,0].forEach((v,i)=>header.writeUInt32LE(v,i*4));
header.writeUInt32LE(0x1b,32);header.writeUInt32LE(24,36);
Buffer.from('0123456789abcdef0123456789abcdef','hex').copy(header,40);
header.writeUInt32LE(0x19,56);header.writeUInt32LE(232,60);header.write('__TEXT',64);
header.writeBigUInt64LE(0n,80);header.writeBigUInt64LE(4096n,88);header.writeBigUInt64LE(0n,96);
header.writeBigUInt64LE(4096n,104);header.writeUInt32LE(5,112);header.writeUInt32LE(5,116);header.writeUInt32LE(2,120);
for(const[i,name,offset,bytes,flags]of [[0,'__text',0x400,0x200,0x80000400],[1,'__const',0x600,0x100,0]]){
 const at=128+i*80;header.write(name,at);header.write('__TEXT',at+16);header.writeBigUInt64LE(BigInt(offset),at+32);
 header.writeBigUInt64LE(BigInt(bytes),at+40);header.writeUInt32LE(offset,at+48);header.writeUInt32LE(flags,at+64);
}
if(claimed!==main){memory.set(0x2000,Buffer.from(header));header[40]^=0xf0;}
header.writeUInt32LE(data.main_subtype??0,8);
if(data.loaded_image)memory.set(codeBase,Buffer.from(data.loaded_image,'base64'));
function backing(address,n){
 for(const[base,bytes]of memory)if(address>=base&&address+n<=base+bytes.length)return[bytes,address-base];
 throw Error('inert unmapped memory');
}
function w32(a,v){const[b,o]=backing(a,4);b.writeUInt32LE(v>>>0,o);}
function w64(a,v){const[b,o]=backing(a,8);b.writeBigUInt64LE(BigInt(v),o);}
for(const owner of [0x3000,0x5000]){w32(owner,1);w64(owner+8,owner+0x1000);w32(owner+0x1000,7);w32(owner+0x1004,41);w64(owner+0x1008,codeBase+0x600);}
if(data.primary_vptr)for(const node of [0x4000,0x6000]){
 w64(node,codeBase+0x600);w32(node+8,7);w32(node+12,41);
}
if(data.borrowed_root)for(const node of [0x3000,0x5000]){
 w64(node,codeBase+(data.vptr_offset??0x600));w32(node+8,7);w32(node+12,41);
}
w64(0x7008,0x3000);
const dataAddress=codeBase+Number(data.data_offset??'0x620');
if(data.data_mode==='object'){w32(dataAddress,1);w64(dataAddress+8,0x4000);}
if(data.data_mode==='pointer')w64(dataAddress,0x3000);
class Pointer{
 constructor(value){this.address=value instanceof Pointer?value.address:Number(value);}
 add(n){return new Pointer(this.address+Number(n));}sub(n){return new Pointer(this.address-Number(n instanceof Pointer?n.address:n));}
 and(n){return new Pointer(BigInt(this.address)&BigInt(n instanceof Pointer?n.address:n));}
 compare(n){return this.address<n.address?-1:this.address>n.address?1:0;}
 isNull(){return this.address===0;}toString(){return '0x'+this.address.toString(16);}
 readU8(){const[b,o]=backing(this.address,1);return b.readUInt8(o);}
 readS8(){const[b,o]=backing(this.address,1);return b.readInt8(o);}
 readU16(){const[b,o]=backing(this.address,2);return b.readUInt16LE(o);}
 readS16(){const[b,o]=backing(this.address,2);return b.readInt16LE(o);}
 readU32(){borrowedRead(this.address,'readU32');
  memoryTrace.push({kind:'read',reader:'readU32',address:this.address,after_mutator:mutatorCalls>0});
  if(data.scenario==='primary_vptr_drift'&&mutatorCalls>0&&this.address===0x4008)throw Error('inert poisoned derived guard');
  if(data.scenario==='read_fault_initial'&&this.address===0x4000)throw Error('inert declared read fault');
  const[b,o]=backing(this.address,4);return b.readUInt32LE(o);}
 readS32(){borrowedRead(this.address,'readS32');
  memoryTrace.push({kind:'read',reader:'readS32',address:this.address,after_mutator:mutatorCalls>0});
  const[b,o]=backing(this.address,4);return b.readInt32LE(o);}
 readU64(){const[b,o]=backing(this.address,8);return{toString:()=>b.readBigUInt64LE(o).toString()};}
 readS64(){const[b,o]=backing(this.address,8);return{toString:()=>b.readBigInt64LE(o).toString()};}
 readPointer(){borrowedRead(this.address,'readPointer');
  memoryTrace.push({kind:'read',reader:'readPointer',address:this.address,after_mutator:mutatorCalls>0});
  const[b,o]=backing(this.address,8);return new Pointer(b.readBigUInt64LE(o));}
 readByteArray(n){const[b,o]=backing(this.address,n);const v=b.subarray(o,o+n);return v.buffer.slice(v.byteOffset,v.byteOffset+v.byteLength);}
 readVolatile(n){return this.readByteArray(n);}
 readUtf8String(n){const[b,o]=backing(this.address,n??1);return b.subarray(o,n===undefined?b.indexOf(0,o):o+n).toString('utf8');}
 writeU32(v){w32(this.address,v);}writeByteArray(v){const[b,o]=backing(this.address,v.length);Buffer.from(v).copy(b,o);}
}
let now=1000,epoch=0,rootCalls=0,mutatorCalls=0,consumerCalls=0,retains=0,releases=0,functionsCreated=0,interventions=0;
let allocation=0x9000,activeRoot=0x3000,reentrant=null;
const calls=[],consumed=[],events=[],discarded=[];
const hookLog=[],listeners=new Map();let currentThread=100,schedules=0,timerSerial=0,flushes=0;
const timers=new Map();
const Interceptor={attach(address,callbacks){
 hookLog.push({kind:'attach',address:address.address});
 if(data.scenario==='lifecycle_install_failure'&&hookLog.filter(row=>row.kind==='attach').length===2)
  throw Error('inert checked online install refused');
 const listener={callbacks,address,detached:false};listeners.set(address.address,listener);
 if(data.scenario==='lifecycle_event_before_armed'&&address.address===codeBase+0x500){
  const frame={threadId:100,returnAddress:new Pointer(codeBase+0x580)};
  callbacks.onEnter.call(frame,[new Pointer(activeRoot)]);callbacks.onLeave.call(frame,new Pointer(0));
 }
 return{detach(){hookLog.push({kind:'detach',address:address.address});
  if(data.scenario==='lifecycle_detach_failure'&&address.address===codeBase+0x500)
   throw Error('inert listener detach uncertain');
  listener.detached=true;
 }};
},flush(){flushes++;hookLog.push({kind:'flush'});
 if(data.scenario==='lifecycle_install_flush_failure'&&flushes===1||
    data.scenario==='lifecycle_cleanup_flush_failure'&&flushes===2)throw Error('inert flush failure');
}};
function lifecycleEnter(offset,address,{site=0x580,thread=100}={}){
 const listener=listeners.get(codeBase+offset);
 if(!listener||listener.detached)throw Error('inert required listener unavailable');
 const invocation={threadId:thread,returnAddress:new Pointer(codeBase+site)};
 const previous=currentThread;currentThread=thread;
 try{listener.callbacks.onEnter.call(invocation,data.scenario==='lifecycle_callback_argument_failure'
  ?[{add(){throw Error('inert lifecycle callback bookkeeping failure');}}]:[new Pointer(address)]);}finally{currentThread=previous;}
 return{listener,invocation,thread};
}
function lifecycleLeave(frame){const previous=currentThread;currentThread=frame.thread;
 try{frame.listener.callbacks.onLeave.call(frame.invocation,new Pointer(0));}finally{currentThread=previous;}}
function birth(address,options){const frame=lifecycleEnter(0x500,address,options);lifecycleLeave(frame);}
function death(address,offset=0x540,thread=100){const frame=lifecycleEnter(offset,address,{thread});lifecycleLeave(frame);}
function naturalLifecycleEvents(){
 const scenario=data.scenario;
 if(['lifecycle_none','lifecycle_preexisting'].includes(scenario))return;
 if(scenario==='lifecycle_pending_constructor'){lifecycleEnter(0x500,activeRoot);return;}
 if(scenario==='lifecycle_wrong_return'){birth(activeRoot,{site:0x5c0});return;}
 if(scenario==='lifecycle_wrong_thread'){birth(activeRoot,{thread:200});return;}
 if(scenario==='lifecycle_unmatched_completion'){
  listeners.get(codeBase+0x500).callbacks.onLeave.call({threadId:100},new Pointer(0));return;
 }
 if(scenario==='lifecycle_nested_constructor'){
  const outer=lifecycleEnter(0x500,activeRoot);birth(0x5000);lifecycleLeave(outer);return;
 }
 birth(activeRoot);
 if(scenario==='lifecycle_two_live'){if(consumerCalls!==0)throw Error('selected before fixed window');birth(0x5000);}
 if(scenario==='lifecycle_same_vptr_death'||scenario==='lifecycle_death')death(activeRoot);
 if(scenario==='lifecycle_unwitnessed_reuse')birth(activeRoot);
 if(scenario==='lifecycle_wrong_site_reuse')birth(activeRoot,{site:0x5c0});
 if(scenario==='lifecycle_witnessed_reuse'){death(activeRoot);birth(activeRoot);}
 if(scenario==='lifecycle_wrong_thread_death')death(activeRoot,0x540,200);
 if(scenario==='lifecycle_pending_destructor')lifecycleEnter(0x540,activeRoot);
 if(scenario==='lifecycle_alias_death'){
  const outer=lifecycleEnter(0x540,activeRoot),inner=lifecycleEnter(0x560,activeRoot);
  lifecycleLeave(inner);lifecycleLeave(outer);birth(0x5000);
 }
 if(scenario==='lifecycle_budget'){death(activeRoot);birth(0x5000);}
 if(scenario==='lifecycle_resource_drift')data.resources[main].sha256='f'.repeat(64);
 if(scenario==='lifecycle_code_base_drift'){
  memory.set(0x8000,Buffer.from(memory.get(codeBase)));module.base=new Pointer(0x8000);
 }
 if(scenario==='lifecycle_vptr_wrong_prefix'){
  w64(activeRoot,codeBase+0x608);memory.set(activeRoot,Buffer.from(memory.get(activeRoot).subarray(0,8)));
 }
 if(scenario==='lifecycle_rpc_reentry')reentrant=context.rpc.exports.run(payload()).then(()=> 'incorrectly admitted',e=>e.message);
}
function deterministicTimer(work,ms){const id=++timerSerial;timers.set(id,true);
 Promise.resolve().then(()=>{if(!timers.has(id))return;naturalLifecycleEvents();
  now+=ms;if(data.scenario==='lifecycle_expired_window')now=10000;
  timers.delete(id);work();});return id;}
function scheduleMain(_queue,work){schedules++;
 if(data.lifecycle_root&&schedules===2){
  if(data.scenario==='lifecycle_selected_death')death(activeRoot);
  if(data.scenario==='lifecycle_selected_new_birth')birth(0x5000);
  if(data.scenario==='lifecycle_wrong_terminal_thread'){currentThread=200;try{return work();}finally{currentThread=100;}}
 }
 return work();
}
function borrowedRead(address,reader){
 if(!data.borrowed_root||![0x3000,0x5000].some(base=>address>=base&&address<base+16))return;
 borrowedTrace.push({address,reader,after_intervention:interventions});
 if(data.poison_after_intervention&&interventions>0)throw Error('inert receiver read after expired epoch');
}
function inertIntervention(){interventions++;return 0;}
const symbols={root:codeBase+(data.getter_offset??0x400),consume:codeBase+(data.consumer_offset??0x440),
 mutate:codeBase+0x480,legacy:codeBase+0x4c0};
const module={path:claimed,name:'Path',base:new Pointer(codeBase),size:data.image_extent??4096,
 findSymbolByName:name=>symbols[name]===undefined?null:module.base.add(symbols[name]-codeBase),findExportByName:()=>null};
const mainModule=claimed===main?module:{path:main,name:'Main',base:new Pointer(0x1000),size:4096};
const Memory={alloc(n){const p=new Pointer(allocation);allocation+=Math.max(n,256);memory.set(p.address,Buffer.alloc(n));return p;}};
const controllerClass={$kind:'class',$className:'Controller',handle:new Pointer(0x7800),$superClass:null};
const controller={$kind:'instance',$className:'Controller',$class:controllerClass,
 handle:new Pointer(data.root_kind==='objc'?0x3000:0x7000),$methods:['- retain','- release'],
 '- retain':()=>retains++,'- release':()=>{releases++;if(data.scenario==='release_fault')throw Error('inert release failure');}};
const text=t=>({isNull:()=>false,readUtf8String:()=>t});
const ObjC={available:true,mainQueue:'main',classes:{Controller:controllerClass},schedule:scheduleMain,
 choose:(_spec,cb)=>{cb.onMatch(controller);cb.onComplete();},api:{class_getImageName:()=>text(main),class_getInstanceSize:()=>24,
 class_copyIvarList(_klass,count){count.writeU32(1);const p=Memory.alloc(8);w64(p.address,0x7900);return p;},
 ivar_getName:()=>text('owner'),ivar_getTypeEncoding:()=>text('^v'),ivar_getOffset:()=>({toString:()=> '8'}),free:()=>{}}};
const Process={id:321,platform:'darwin',arch:'arm64',pointerSize:8,mainModule,getCurrentThreadId:()=>currentThread,
 enumerateModules:()=>claimed===main?[module]:[mainModule,module],
 findModuleByAddress:p=>p.address>=module.base.address&&p.address<module.base.address+module.size?module:
  p.address>=mainModule.base.address&&p.address<mainModule.base.address+mainModule.size?mainModule:null,
 findRangeByAddress(p){memoryTrace.push({kind:'range',address:p.address,after_mutator:mutatorCalls>0});
  for(const[base,b]of memory)if(p.address>=base&&p.address<base+b.length)
   return{base:new Pointer(base),size:b.length,protection:[0x1000,0x2000,0x8000,codeBase].includes(base)?'r-x':'rw-'};return null;}};
function mutate(){
 const scenario=data.scenario;
 if(scenario==='edge_drift')w64(0x3008,0x6000);
 if(scenario==='null_drift')w64(0x3008,0);
 if(scenario==='guard_drift')w32(0x4000,8);
 if(scenario==='bool_drift')w32(0x3000,2);
 if(scenario==='vptr_drift')w64(0x4008,0x1608);
 if(scenario==='primary_vptr_drift')w64(0x4000,codeBase+0x608);
 if(scenario==='root_slot_drift')w64(0x7008,0x5000);
 if(scenario==='data_slot_drift')w64(dataAddress,0x5000);
 if(scenario==='data_edge_drift')w64(dataAddress+8,0x6000);
 if(scenario==='data_base_drift'){
  // Keep the old image and pointees readable; only the claimed loaded base moves.
  memory.set(0x8000,Buffer.from(memory.get(codeBase)));module.base=new Pointer(0x8000);
 }
 if(scenario==='unmapped_drift')memory.delete(0x4000);
 if(scenario==='uuid_drift')header[40]^=1;
 if(scenario==='resource_drift')data.resources[main].sha256='f'.repeat(64);
 if(scenario==='recipe_drift')data.operations[0].arguments[0].value.native_path.preconditions[1].equals=8;
 if(scenario==='call_drift')data.operations[0].target.symbol='legacy';
 if(scenario==='deadline')now=5000;
}
function NativeFunction(address,_ret,_args,options){
 functionsCreated++;
 if(options.abi!=='default'||options.exceptions!=='steal')throw Error('inert unexpected native ABI');
 return(...values)=>{
  calls.push(address.address);
  if(address.address===symbols.root){rootCalls++;return new Pointer(activeRoot+(data.paths>1&&(rootCalls-1)%data.paths?0x2000:0));}
  if(address.address===symbols.mutate){mutatorCalls++;mutate();return new Pointer(0x4000);}
  if(address.address===symbols.legacy)return values[0][0]+values[1];
  if(address.address!==symbols.consume)throw Error('inert unknown native function');
  consumerCalls++;consumed.push(values[0].address);
  if(data.scenario==='consumer_fault')throw Error('inert target fault');
  if(data.receiver_hypothesis){
   if(data.scenario==='receiver_discard_after_consumer'){
    memory.delete(values[0].address);discarded.push(values[0].address);
   }
   return 41+values[values.length-1];
  }
  const value=values[0].add(data.primary_vptr||data.borrowed_root?12:4).readS32()+values[values.length-1];
  if(data.scenario==='borrowed_discard_after_consumer'){
   memory.delete(values[0].address);discarded.push(values[0].address);
  }
  if(data.scenario==='lifecycle_discard_after_consumer'){
   death(values[0].address);memory.delete(values[0].address);discarded.push(values[0].address);
  }
  return value;
 };
}
let context;
function send(event){
 events.push(JSON.parse(JSON.stringify(event)));
 if(data.scenario==='reentry'&&event.event==='native-path-resolved'&&reentrant===null)
  reentrant=context.rpc.exports.run(payload()).then(()=> 'incorrectly admitted',e=>e.message);
 if(data.scenario==='last_getter_edge'&&event.event==='native-invoke-start')w64(0x3008,0x6000);
 if(data.scenario==='borrowed_read_selection_drift'&&event.event==='native-path-resolved')
  data.operations[0].fields.push({name:'payload',type:'int32',offset:12});
 if(data.scenario==='receiver_vptr_drift'&&event.event==='native-path-resolved')w64(0x3000,codeBase+0x608);
 if(data.scenario==='receiver_resource_drift'&&event.event==='native-path-resolved')
  data.resources['/fixture/path.json'].sha256='f'.repeat(64);
}
context=vm.createContext({ObjC,Memory,Process,NativeFunction,ptr:x=>new Pointer(x),rpc:{exports:{}},send,
 Interceptor,setTimeout:deterministicTimer,clearTimeout:id=>timers.delete(id),
 Checksum:{compute:(algorithm,value)=>crypto.createHash(algorithm).update(value,'utf8').digest('hex')},
 inertIntervention,Date:{now:()=>now},Map,Set,WeakMap,Promise,Error,Uint8Array,
 int64:x=>({toString:()=>x}),uint64:x=>({toString:()=>x})});
vm.runInContext(data.source,context,{timeout:1000});
function payload(){return{request:{operations:data.operations},native_resources:data.resources,deadline_ms:data.deadline_ms??(data.lifecycle_root?10000:4000),
 pid:321,bundle:'/fixture/Path.app',executable:main};}
if(data.scenario==='null_initial')w64(0x3008,0);
if(data.scenario==='misaligned_initial')w64(0x3008,0x4001);
if(data.scenario==='cycle_initial')w64(0x3008,0x3000);
if(data.scenario==='bool_initial')w32(0x3000,2);
if(data.scenario==='guard_initial')w32(0x4000,8);
if(data.scenario==='short_initial')memory.set(0x4000,Buffer.alloc(8));
if(data.scenario==='primary_vptr_wrong_prefix')w64(0x4000,codeBase+0x608);
if(['primary_vptr_wrong_prefix','primary_vptr_matching_prefix'].includes(data.scenario))
 memory.set(0x4000,Buffer.from(memory.get(0x4000).subarray(0,8)));
if(data.scenario==='borrowed_wrong_prefix'){
 w64(0x3000,codeBase+0x608);memory.set(0x3000,Buffer.from(memory.get(0x3000).subarray(0,8)));
}
if(data.scenario==='uuid_initial')header[40]^=1;
if(data.scenario==='data_null_initial')w64(dataAddress,0);
if(data.scenario==='data_misaligned_initial')w64(dataAddress,0x3001);
if(data.scenario==='data_zero_fill_initial')memory.get(codeBase).writeUInt32LE(1,272);
if(data.scenario==='data_instruction_initial')memory.get(codeBase).writeUInt32LE(0x80000400,272);
if(data.scenario==='data_file_tail_initial')memory.get(codeBase).writeBigUInt64LE(0x680n,104);
if(data.scenario==='data_file_mapping_initial')memory.get(codeBase).writeUInt32LE(0x608,256);
if(data.scenario==='data_short_section_initial')memory.get(codeBase).writeBigUInt64LE(0xfcn,248);
if(data.scenario==='data_child_null_initial')w64(data.data_mode==='object'?dataAddress+8:0x3008,0);
if(data.scenario==='data_overlap_initial'){
 const h=memory.get(codeBase);h.writeBigUInt64LE(BigInt(dataAddress-codeBase+4),160);h.writeBigUInt64LE(8n,168);
}
(async()=>{
 const results=[await context.rpc.exports.run(payload())];
 let stale=null;
 if(data.scenario==='stale_permit'){try{context.pathProbe();stale='incorrectly admitted';}catch(e){stale=e.message;}}
 if(data.repeat){epoch++;activeRoot=0x5000;controller.handle=new Pointer(data.root_kind==='objc'?0x5000:0x7000);
  w64(0x7008,0x5000);
  if(data.data_mode==='object')w64(dataAddress+8,0x6000);
  if(data.data_mode==='pointer')w64(dataAddress,0x5000);
  results.push(await context.rpc.exports.run(payload()));}
 process.stdout.write(JSON.stringify({results,rootCalls,mutatorCalls,consumerCalls,retains,releases,functionsCreated,
  calls,consumed,events,memoryTrace,borrowedTrace,interventions,discarded,hookLog,
  reentrant:reentrant===null?null:await reentrant,stale}));
})().catch(e=>{process.stderr.write(String(e.stack));process.exitCode=1;});
"""


def _run(case, scenario="stable", *, repeat=False, instrument=None, source_copy=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("inert runtime fixture requires an installed Node")
    source = Path(B.__file__).with_name("rea_runtime_agent.js").read_text()
    assert source.count("import ObjC from 'frida-objc-bridge';") == 1
    source = source.replace("import ObjC from 'frida-objc-bridge';", "", 1)
    if instrument:
        # Fault injection exists only in this inert source copy; no product hook.
        needle = "return targetCall(found.function_, arguments_, pathCall, rootCapture);"
        assert source.count(needle) == 1
        source = source.replace(needle, "if (pathCall !== null) {\n" + instrument + "\n}\n" + needle, 1)
    for needle, replacement in (source_copy or {}).items():
        # Internal transition faults exist only in this inert source copy. The
        # deployed adapter has no test hook or caller-supplied source facility.
        assert source.count(needle) == 1
        source = source.replace(needle, replacement, 1)
    data = {**copy.deepcopy(case), "source": source, "scenario": scenario, "repeat": repeat}
    output = subprocess.run([node, "-e", _FIXTURE], input=json.dumps(data, allow_nan=False),
                            capture_output=True, text=True, timeout=20, check=False)
    assert output.returncode == 0, output.stderr
    return json.loads(output.stdout)


def _refused(output, *, calls=None, uncertain=False):
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"]
    assert result["invocations"] == 0 and output["consumerCalls"] == 0
    assert result["handles_expired"] is True and result["native_state_uncertain"] is uncertain
    assert result["helper_load_attempts"] == 0 and output["retains"] == output["releases"]
    if calls is not None:
        assert result["native_calls"] == calls
    return result


def _primary_vptr_trace(output):
    return [event for event in output["memoryTrace"] if 0x4000 <= event["address"] < 0x4010]


def test_primary_vptr_mismatch_refuses_before_the_unreadable_derived_extent_or_fields():
    case, _ = _primary_vptr_case()
    output = _run(case, "primary_vptr_wrong_prefix")
    result = _refused(output, calls=1)
    assert result["error"]["message"] == "native path pinned data identity differs from its exact non-instruction section"
    # Only the8-byte prefix exists. A full-extent check would produce a
    # different refusal; any derived read would be outside mapped memory.
    assert _primary_vptr_trace(output) == [
        {"kind": "range", "address": 0x4000, "after_mutator": False},
        {"kind": "read", "reader": "readPointer", "address": 0x4000, "after_mutator": False}]
    assert output["rootCalls"] == 1 and output["mutatorCalls"] == 0 and result["operations"] == []


def test_matching_primary_vptr_does_not_admit_an_unreadable_derived_extent():
    case, _ = _primary_vptr_case()
    output = _run(case, "primary_vptr_matching_prefix")
    result = _refused(output, calls=1)
    assert result["error"]["message"] == "declared native memory region is not wholly readable"
    assert _primary_vptr_trace(output) == [
        {"kind": "range", "address": 0x4000, "after_mutator": False},
        {"kind": "read", "reader": "readPointer", "address": 0x4000, "after_mutator": False},
        {"kind": "range", "address": 0x4000, "after_mutator": False}]
    assert output["rootCalls"] == 1 and output["mutatorCalls"] == 0 and result["operations"] == []


def test_primary_vptr_drift_at_seal_refuses_before_a_poisoned_derived_guard():
    case, _ = _primary_vptr_case()
    output = _run(case, "primary_vptr_drift")
    result = _refused(output, calls=2)
    assert result["error"]["message"] == "native path pinned data identity differs from its exact non-instruction section"
    assert output["rootCalls"] == output["mutatorCalls"] == 1 and output["calls"] == [0x1400, 0x1480]
    trace = _primary_vptr_trace(output)
    assert [(event["reader"], event["address"]) for event in trace if event["kind"] == "read"] == [
        ("readPointer", 0x4000), ("readU32", 0x4008), ("readPointer", 0x4000)]
    assert [event for event in trace if event["after_mutator"]] == [
        {"kind": "range", "address": 0x4000, "after_mutator": True},
        {"kind": "read", "reader": "readPointer", "address": 0x4000, "after_mutator": True}]
    paths = [event["observation"] for event in output["events"] if event["event"] == "native-path-resolved"]
    assert len(paths) == 1 and paths[0]["sealed"] is paths[0]["dispatch_permit_consumed"] is False
    assert result["operations"] == []


def test_matching_primary_vptr_is_read_once_before_derived_state_at_resolution_and_seal():
    case, recipes = _primary_vptr_case()
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == 3 and result["invocations"] == 1
    assert output["calls"] == [0x1400, 0x1480, 0x1440] and output["consumerCalls"] == 1
    assert result["operations"][0]["value"] == 64 and result["handles_expired"] is True
    P.validate_observations(result["operations"][0]["native_paths"], recipes)
    trace = _primary_vptr_trace(output)
    for after_mutator in (False, True):
        expected = [
            {"kind": "range", "address": 0x4000, "after_mutator": after_mutator},
            {"kind": "read", "reader": "readPointer", "address": 0x4000, "after_mutator": after_mutator},
            {"kind": "range", "address": 0x4000, "after_mutator": after_mutator},
            {"kind": "range", "address": 0x4008, "after_mutator": after_mutator},
            {"kind": "read", "reader": "readU32", "address": 0x4008, "after_mutator": after_mutator}]
        if after_mutator:
            expected.append({"kind": "read", "reader": "readS32", "address": 0x400c, "after_mutator": True})
        assert [event for event in trace if event["after_mutator"] is after_mutator] == expected


@pytest.mark.parametrize("root_kind", ["getter", "objc", "read"])
def test_actual_fixed_js_resolves_fresh_roots_dispatches_once_and_expires_borrowed_handles(root_kind):
    case, recipes = _case(root_kind=root_kind, later_getter=False, identity=True)
    saved = copy.deepcopy(case)
    output = _run(case, repeat=True)
    assert case == saved and output["consumerCalls"] == 2 and output["consumed"] == [0x4000, 0x6000]
    assert output["rootCalls"] == (2 if root_kind == "getter" else 0)
    assert output["retains"] == output["releases"] == (0 if root_kind == "getter" else 2)
    for result in output["results"]:
        assert result["status"] == "completed" and result["invocations"] == 1
        assert result["native_calls"] == (2 if root_kind == "getter" else 1)
        assert result["handles_expired"] and result["helper_load_attempts"] == 0
        row = result["operations"][0]
        assert row["value"] == 64
        assert P.validate_observations(row["native_paths"], recipes) == row["native_paths"]
        for hop in row["native_paths"][0]["hops"]:
            assert set(hop["pointer"]) == {"handle", "alias", "ownership", "expires"}
            assert hop["pointer"]["handle"].startswith("n") and hop["pointer"]["alias"] is None


@pytest.mark.parametrize("main_subtype,accepted", [(0, True), (2, False)])
def test_actual_main_image_abi_is_checked_even_when_only_a_different_plain_dylib_is_claimed(main_subtype, accepted):
    dylib = "/fixture/Path.app/Contents/Frameworks/Objects.dylib"
    case, recipes = _case(later_getter=False, identity=True, image_path=dylib)
    assert MAIN not in case["resources"] and case["resources"][dylib]["macho_images"][0]["cpu_subtype"] == 0
    case["main_subtype"] = main_subtype
    output = _run(case)
    if not accepted:
        result = _refused(output, calls=0)
        assert "actual plain-arm64 main image" in result["error"]["message"]
        assert output["functionsCreated"] == output["rootCalls"] == output["consumerCalls"] == 0
    else:
        result = output["results"][0]
        assert result["status"] == "completed" and result["native_calls"] == 2 and result["invocations"] == 1
        assert output["calls"] == [0x2400, 0x2440] and output["consumerCalls"] == output["rootCalls"] == 1
        P.validate_observations(result["operations"][0]["native_paths"], recipes)
        assert result["handles_expired"]


@pytest.mark.parametrize("scenario", ["edge_drift", "null_drift", "guard_drift", "bool_drift", "vptr_drift",
    "unmapped_drift", "uuid_drift", "resource_drift", "recipe_drift", "call_drift", "last_getter_edge", "deadline"])
def test_all_getters_finish_before_sealing_and_any_drift_refuses_without_reacquisition(scenario):
    case, _ = _case(identity=True)
    output = _run(case, scenario)
    _refused(output, calls=2)
    assert output["rootCalls"] == output["mutatorCalls"] == 1
    assert output["calls"] == [0x1400, 0x1480]


def test_multiple_paths_are_all_sealed_after_the_last_argument_getter():
    case, recipes = _case(paths=2)
    good = _run(case)
    result = good["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == 4 and result["invocations"] == 1
    P.validate_observations(result["operations"][0]["native_paths"], recipes)
    assert good["calls"] == [0x1400, 0x1400, 0x1480, 0x1440]
    bad = _run(case, "edge_drift")
    _refused(bad, calls=3)
    assert bad["rootCalls"] == 2 and bad["mutatorCalls"] == 1


@pytest.mark.parametrize("scenario", ["null_initial", "misaligned_initial", "cycle_initial", "bool_initial",
    "guard_initial", "short_initial", "read_fault_initial"])
def test_null_layout_guard_and_memory_failures_never_reach_later_getters_or_consumer(scenario):
    case, _ = _case(identity=True)
    output = _run(case, scenario)
    _refused(output, calls=1)
    assert output["rootCalls"] == 1 and output["mutatorCalls"] == 0


@pytest.mark.parametrize("missing", ["lifetime", "original", "contract_correlation", "data_section", "uuid"])
def test_independent_lifetime_original_and_image_pins_are_checked_before_any_root_code(missing):
    case, _ = _case(identity=True)
    if missing == "lifetime":
        case["resources"]["/fixture/path.json"]["native_path_contracts"] = [
            row for row in case["resources"]["/fixture/path.json"]["native_path_contracts"] if row["pointer"] != "/lifetime"]
    elif missing == "original":
        del case["resources"]["/fixture/objects.h"]
    elif missing == "contract_correlation":
        case["resources"]["/fixture/path.json"]["native_path_contracts"][0]["originals"][0]["sha256"] = "f" * 64
    elif missing == "data_section":
        del case["resources"][MAIN]["native_path_data_sections"]
    output = _run(case, "uuid_initial" if missing == "uuid" else "stable")
    _refused(output, calls=0)
    assert output["functionsCreated"] == output["rootCalls"] == output["mutatorCalls"] == 0


@pytest.mark.parametrize("attack", ["copied", "slot", "call", "transaction", "reused", "function", "argument_object"])
def test_private_one_shot_permits_are_bound_to_actual_call_slot_and_transaction(attack):
    case, _ = _case(later_getter=False)
    injection = {
        "copied": "nativePathCalls.get(pathCall).permits[0] = Object.freeze({});",
        "slot": "nativePathPermits.get(nativePathCalls.get(pathCall).permits[0]).argument = 1;",
        "call": "nativePathPermits.get(nativePathCalls.get(pathCall).permits[0]).call = Object.freeze({});",
        "transaction": "nativePathPermits.get(nativePathCalls.get(pathCall).permits[0]).transaction = Object.freeze({});",
        "reused": "consumeNativePathPermits(pathCall, found.function_, arguments_);",
        "function": "return targetCall(() => 0, arguments_, pathCall);",
        "argument_object": "nativePathCalls.get(pathCall).argumentObjects[0] = {...specification.arguments[0]};",
    }[attack]
    output = _run(case, instrument=injection)
    _refused(output, calls=1)
    assert output["rootCalls"] == 1


def test_permits_expire_at_transaction_exit_and_rpc_reentry_cannot_share_a_lifetime():
    case, _ = _case(later_getter=False)
    stale = _run(case, "stale_permit", instrument=
                 "globalThis.pathProbe = () => consumeNativePathPermits(pathCall, found.function_, arguments_);")
    assert stale["results"][0]["status"] == "completed" and stale["consumerCalls"] == 1
    assert "not current" in stale["stale"]
    reentry = _run(case, "reentry")
    assert reentry["results"][0]["status"] == "completed" and reentry["consumerCalls"] == 1
    assert "reentered during a borrowed native path lifetime" in reentry["reentrant"]


@pytest.mark.parametrize("root_kind", ["objc", "read"])
def test_owner_cleanup_covers_refusal_and_post_entry_fault_without_child_retains(root_kind):
    case, _ = _case(root_kind=root_kind)
    denied = _run(case, "edge_drift")
    _refused(denied, calls=1)
    assert denied["retains"] == denied["releases"] == 1
    fault = _run(case, "consumer_fault")
    result = fault["results"][0]
    assert result["status"] == "incomplete" and result["native_state_uncertain"] is True
    assert result["invocations"] == 1 and result["native_calls"] == 2 and fault["consumerCalls"] == 1
    assert result["handles_expired"] and fault["retains"] == fault["releases"] == 1


def test_retained_read_owner_does_not_allow_a_changed_root_slot_or_hide_release_fault():
    case, _ = _case(root_kind="read")
    refused = _run(case, "root_slot_drift")
    _refused(refused, calls=1)
    assert "root slot/lifecycle guard changed" in refused["results"][0]["error"]["message"]
    fault = _run(case, "release_fault")
    result = fault["results"][0]
    assert result["status"] == "incomplete" and result["error"]["type"] == "LifetimeError"
    assert result["native_state_uncertain"] and result["handles_expired"]
    assert fault["consumerCalls"] == 1 and fault["retains"] == fault["releases"] == 1


@pytest.mark.parametrize("operation", ["native_read", "graph"])
def test_path_investigation_uses_existing_read_graph_routes_without_dispatch_permits(operation):
    case, recipes = _case(later_getter=False)
    recipe = recipes[0]
    recipe.pop("consumer")
    layout = recipe["types"][1]
    projection = {key: copy.deepcopy(layout[key]) for key in ("name", "bytes", "fields")}
    legacy_layout = {"kind": "header", "file": "/fixture/abi.json", "sha256": "b" * 64,
                     "pointer": "/legacy-result-layout"}
    projection["layout_evidence"] = legacy_layout
    if operation == "native_read":
        case["operations"] = [{"op": operation, "receiver": {"native_path": recipe},
                                "bytes": 16, "fields": projection["fields"], "layout_evidence": legacy_layout}]
    else:
        case["operations"] = [{"op": operation, "root": {"native_path": recipe}, "root_type": "Node",
                                "types": [projection], "max_depth": 1, "max_nodes": 4, "methods_limit": 0}]
    B.validate_request({"operations": case["operations"]})
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == 1 and result["invocations"] == 0
    assert output["rootCalls"] == 1 and output["consumerCalls"] == output["mutatorCalls"] == 0
    row = result["operations"][0]
    P.validate_observations(row["native_paths"], recipes, for_call=False)
    if operation == "native_read":
        assert [field["value"] for field in row["fields"]] == [7, 41]
    else:
        assert row["errors"] == [] and row["evidence_only"] is True and row["verified"] is False
        assert row["nodes"][0]["type"] == "Node" and row["getter_calls"] == 1
    assert result["handles_expired"]


@pytest.mark.parametrize("data_mode,data_offset", [
    ("object", "0x620"), ("object", "0x6f0"), ("pointer", "0x650"), ("pointer", "0x6f8")])
def test_data_roots_resolve_fresh_original_locations_without_root_calls_or_retains(data_mode, data_offset):
    case, recipes = _case(data_mode=data_mode, data_offset=data_offset, later_getter=False, identity=True)
    saved = copy.deepcopy(case)
    output = _run(case, repeat=True)
    assert case == saved and output["consumed"] == [0x4000, 0x6000]
    assert output["rootCalls"] == output["mutatorCalls"] == output["retains"] == output["releases"] == 0
    assert output["consumerCalls"] == 2 and output["calls"] == [0x1440, 0x1440]
    for result in output["results"]:
        assert result["status"] == "completed" and result["native_calls"] == result["invocations"] == 1
        row = result["operations"][0]
        assert row["value"] == 64 and len(row["native_paths"]) == 1
        path = row["native_paths"][0]
        assert path["root_kind"] == "data" and path["owner_retained"] is False
        assert path["lifetime_mode"] == "process_lifetime" and path["sealed"] and path["dispatch_permit_consumed"]
        P.validate_observations(row["native_paths"], recipes)
        assert result["handles_expired"] and result["helper_load_attempts"] == 0


@pytest.mark.parametrize("data_mode", ["object", "pointer"])
@pytest.mark.parametrize("operation", ["native_read", "graph"])
def test_data_root_investigation_has_zero_target_calls_and_cannot_mint_dispatch_proof(data_mode, operation):
    case, recipes = _case(data_mode=data_mode, later_getter=False)
    recipe = recipes[0]
    recipe.pop("consumer")
    layout = recipe["types"][1]
    legacy_layout = {"kind": "header", "file": "/fixture/abi.json", "sha256": "b" * 64,
                     "pointer": "/legacy-result-layout"}
    if operation == "native_read":
        case["operations"] = [{"op": operation, "receiver": {"native_path": recipe}, "bytes": layout["bytes"],
                                "fields": copy.deepcopy(layout["fields"]), "layout_evidence": legacy_layout}]
    else:
        projection = {key: copy.deepcopy(layout[key]) for key in ("name", "bytes", "fields")}
        projection["layout_evidence"] = legacy_layout
        case["operations"] = [{"op": operation, "root": {"native_path": recipe}, "root_type": "Node",
                                "types": [projection], "max_depth": 1, "max_nodes": 4, "methods_limit": 0}]
    B.validate_request({"operations": case["operations"]})
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == result["invocations"] == 0
    assert output["rootCalls"] == output["mutatorCalls"] == output["consumerCalls"] == output["functionsCreated"] == 0
    assert output["retains"] == output["releases"] == 0
    row = result["operations"][0]
    P.validate_observations(row["native_paths"], recipes, for_call=False)
    with pytest.raises(ValueError):
        P.validate_observations(row["native_paths"], recipes)
    assert row["native_paths"][0]["owner_retained"] is False
    assert row["native_paths"][0]["sealed"] is row["native_paths"][0]["dispatch_permit_consumed"] is False
    if operation == "native_read":
        assert [field["value"] for field in row["fields"]] == [7, 41]
    else:
        assert row["errors"] == [] and row["getter_calls"] == 0 and row["potentially_mutating_getters"] is False
        assert row["evidence_only"] is True and row["verified"] is False
    assert result["handles_expired"]


@pytest.mark.parametrize("scenario", ["data_null_initial", "data_misaligned_initial", "data_child_null_initial"])
def test_data_pointer_root_null_or_stale_pointee_never_reaches_later_getters(scenario):
    case, _ = _case(data_mode="pointer")
    output = _run(case, scenario)
    _refused(output, calls=0)
    assert output["rootCalls"] == output["mutatorCalls"] == output["consumerCalls"] == 0
    assert output["retains"] == output["releases"] == 0


@pytest.mark.parametrize("data_mode,scenario", [
    ("object", "data_edge_drift"), ("pointer", "data_slot_drift"),
    ("object", "data_base_drift"), ("pointer", "data_base_drift")])
def test_data_root_slot_edge_and_loaded_base_drift_refuse_without_reacquisition(data_mode, scenario):
    case, _ = _case(data_mode=data_mode)
    output = _run(case, scenario)
    result = _refused(output, calls=1)
    assert output["rootCalls"] == 0 and output["mutatorCalls"] == 1 and output["calls"] == [0x1480]
    assert output["retains"] == output["releases"] == 0 and output["consumed"] == []
    if scenario == "data_slot_drift":
        assert "root slot/lifecycle guard changed" in result["error"]["message"]
    elif scenario == "data_base_drift":
        assert "data root image/location changed" in result["error"]["message"]
    else:
        assert "selected pointer/guard changed" in result["error"]["message"]


@pytest.mark.parametrize("data_mode", ["object", "pointer"])
@pytest.mark.parametrize("failure", ["missing_pin", "zero_fill", "instruction", "file_tail", "file_mapping",
    "loaded_overlap", "original_overlap", "duplicate_section", "duplicate_image", "section_boundary"])
def test_data_location_requires_one_whole_original_file_backed_noninstruction_section(data_mode, failure):
    offset = "0x6f8" if failure == "section_boundary" else None
    case, recipes = _case(data_mode=data_mode, data_offset=offset, later_getter=False)
    pin = case["resources"][MAIN]
    row = pin["native_path_data_sections"][0]
    scenario = "stable"
    if failure == "missing_pin":
        del pin["native_path_data_sections"]
    elif failure == "zero_fill":
        row["sections"][0]["flags"] = 1
        scenario = "data_zero_fill_initial"
    elif failure == "instruction":
        row["sections"][0]["flags"] = 0x80000400
        scenario = "data_instruction_initial"
    elif failure == "file_tail":
        pin["macho_segments"][0]["segments"][0]["file_bytes"] = 0x680
        scenario = "data_file_tail_initial"
    elif failure == "file_mapping":
        scenario = "data_file_mapping_initial"
    elif failure == "loaded_overlap":
        scenario = "data_overlap_initial"
    elif failure == "original_overlap":
        row["sections"].append({"segment": "__TEXT", "name": "overlap",
            "offset": hex(int(case["data_offset"], 16) + 4), "bytes": 8, "flags": 0})
    elif failure == "duplicate_section":
        row["sections"].append(copy.deepcopy(row["sections"][0]))
    elif failure == "duplicate_image":
        pin["native_path_data_sections"].append(copy.deepcopy(row))
    elif failure == "section_boundary" and data_mode == "pointer":
        row["sections"][0]["bytes"] = 0xfc
        scenario = "data_short_section_initial"
    output = _run(case, scenario)
    _refused(output, calls=0)
    assert output["functionsCreated"] == output["rootCalls"] == output["mutatorCalls"] == output["consumerCalls"] == 0
    assert output["retains"] == output["releases"] == 0


@pytest.mark.parametrize("data_mode", ["object", "pointer"])
@pytest.mark.parametrize("offset", ["0x400", "0x800", "0x621", "0x20000000000000", "0x0620", "0X620", "-0x8"])
def test_data_root_offsets_cannot_select_code_holes_unaligned_or_noncanonical_locations(data_mode, offset):
    case, recipes = _case(data_mode=data_mode, later_getter=False)
    recipes[0]["root"]["data"]["offset"] = offset
    _refresh_contract_pins(case, recipes)
    output = _run(case)
    _refused(output, calls=0)
    assert output["functionsCreated"] == output["rootCalls"] == output["mutatorCalls"] == 0


@pytest.mark.parametrize("failure", ["extra", "alias", "raw_address", "symbol", "owner_retained", "module_extra",
    "missing_module", "offset_number", "mode", "retained_lifetime", "root_alias"])
def test_data_roots_refuse_aliases_extra_keys_and_retained_owner_lifetime_even_with_matching_contracts(failure):
    case, recipes = _case(data_mode="object", later_getter=False)
    recipe = recipes[0]
    data = recipe["root"]["data"]
    if failure == "extra":
        data["unexpected"] = 0
    elif failure == "alias":
        data["alias"] = "saved"
    elif failure == "raw_address":
        data["address"] = "0x1620"
    elif failure == "symbol":
        data["symbol"] = "root"
    elif failure == "owner_retained":
        data["owner_retained"] = True
    elif failure == "module_extra":
        data["module"]["alias"] = "saved"
    elif failure == "missing_module":
        del data["module"]
    elif failure == "offset_number":
        data["offset"] = 0x620
    elif failure == "mode":
        data["mode"] = "borrowed"
    elif failure == "retained_lifetime":
        recipe["lifetime"]["mode"] = "retained_owner_main_queue_stable"
    else:
        recipe["root"]["alias"] = "saved"
    _refresh_contract_pins(case, recipes)
    output = _run(case)
    result = _refused(output, calls=0)
    assert "native path" in result["error"]["message"]
    assert output["functionsCreated"] == output["rootCalls"] == output["mutatorCalls"] == output["retains"] == 0


@pytest.mark.parametrize("data_mode", ["object", "pointer"])
def test_data_location_is_never_a_generic_pointer_argument_fallback(data_mode):
    case, recipes = _case(data_mode=data_mode, later_getter=False)
    case["operations"][0]["arguments"][0]["value"] = copy.deepcopy(recipes[0]["root"])
    with pytest.raises(ValueError):
        B.validate_request({"operations": case["operations"]})
    output = _run(case)
    _refused(output, calls=0)
    assert output["rootCalls"] == output["mutatorCalls"] == output["retains"] == 0


def test_ordinary_pod_field_named_native_path_keeps_the_legacy_native_route():
    case, _ = _case(later_getter=False)
    operation = case["operations"][0]
    operation["target"]["symbol"] = "legacy"
    operation["arguments"] = [{"type": {"kind": "struct", "fields": [{"name": "native_path", "type": "int32"}]},
                               "value": {"native_path": 5}}, {"type": "int32", "value": 7}]
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["operations"][0]["value"] == 12
    assert result["native_calls"] == result["invocations"] == 1
    assert "native_paths" not in result["operations"][0] and output["rootCalls"] == 0


@pytest.mark.parametrize("return_type", ["int32", "void"])
def test_receiver_hypothesis_dispatch_reacquires_fresh_receiver_and_labels_its_unproven_basis(return_type):
    case, recipes = _receiver_hypothesis_case(return_type=return_type)
    saved = copy.deepcopy(case)
    output = _run(case, repeat=True)
    assert case == saved and output["rootCalls"] == output["consumerCalls"] == 2
    assert output["mutatorCalls"] == output["retains"] == output["releases"] == 0
    assert output["consumed"] == [0x3000, 0x5000] and output["calls"] == [0x1400, 0x1440, 0x1400, 0x1440]
    for result in output["results"]:
        assert result["status"] == "completed" and result["native_calls"] == 2 and result["invocations"] == 1
        assert result["handles_expired"] and result["error"] is None and result["native_state_uncertain"] is False
        row = result["operations"][0]
        assert row["value"] == (64 if return_type == "int32" else None) and len(row["native_paths"]) == 1
        path = row["native_paths"][0]
        assert P.validate_observations(row["native_paths"], recipes) == row["native_paths"]
        assert path["evidence_basis"] == NX.BASIS and path["owner_retained"] is False
        assert path["borrowed_scope"] == {"getter_epoch": 1, "checked_epoch": 1,
            "use": "dispatch", "consumed": True, "expires": "use"}
        assert path["hops"][0]["edge"] is None and path["hops"][0]["guards"] == []
    contracts = NX.contracts(recipes[0])
    assert "signature_evidence" not in json.dumps(contracts) and "independently_excluded" not in json.dumps(contracts)


@pytest.mark.parametrize("reverse_signatures", [False, True])
def test_receiver_hypothesis_accepts_actual_request_resource_merge_in_either_signature_order(actual_receiver_declaration,
                                                                                           reverse_signatures):
    _, _, declared, image = actual_receiver_declaration
    actor = B.bind_actor(declared["actor"], {"amount": "7"})
    request = B.invocation_request(actor)
    resources = B.request_resources(request)
    signatures = resources[declared["receipt"]]["native_receiver_signatures"]
    assert {row["pointer"] for row in signatures} == {"/signatures/getter", "/signatures/consumer"}
    if reverse_signatures:
        signatures.reverse()
    pin = resources[str(image)]
    extent = max(int(row["offset"], 16) + row["bytes"] for row in pin["macho_segments"][0]["segments"])
    case = {"operations": request["operations"], "resources": resources, "main_path": str(image),
            "claimed_module": str(image), "image_base": 0x100000, "image_extent": extent,
            "loaded_image": base64.b64encode(image.read_bytes()).decode("ascii"),
            "getter_offset": 0x1000, "consumer_offset": 0x1004, "vptr_offset": 0x2000,
            "root_kind": "getter", "paths": 1, "borrowed_root": True, "receiver_hypothesis": True}
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["error"] is None
    assert result["native_calls"] == 2 and result["invocations"] == 1 and result["handles_expired"] is True
    assert output["rootCalls"] == output["consumerCalls"] == 1 and output["calls"] == [0x101000, 0x101004]
    assert output["retains"] == output["releases"] == output["mutatorCalls"] == 0
    recipe = actor["arguments"][0]["value"]["native_path"]
    P.validate_observations(result["operations"][0]["native_paths"], [recipe])


@pytest.mark.parametrize("scenario", ["borrowed_wrong_prefix", "receiver_vptr_drift", "receiver_resource_drift"])
def test_receiver_hypothesis_rechecks_vptr_and_pins_before_the_immediate_consumer(scenario):
    case, _ = _receiver_hypothesis_case()
    output = _run(case, scenario)
    _refused(output, calls=1)
    assert output["rootCalls"] == 1 and output["calls"] == [0x1400] and output["consumed"] == []
    assert all(row["reader"] == "readPointer" for row in output["borrowedTrace"])
    if scenario == "borrowed_wrong_prefix":
        assert len(output["borrowedTrace"]) == 1


@pytest.mark.parametrize("placement", ["extra_before", "extra_after", "alias", "context", "unsupported_arg", "nested"])
def test_receiver_hypothesis_rejects_extra_work_and_escape_routes_before_its_getter(placement):
    case, recipes = _receiver_hypothesis_case()
    recipe, operation = recipes[0], case["operations"][0]
    if placement in {"extra_before", "extra_after"}:
        case["operations"].insert(0 if placement == "extra_before" else 1, {"op": "modules"})
    elif placement == "alias":
        operation["alias"] = "escaped"
    elif placement == "context":
        operation["context"] = {"type": "pointer", "value": {"null": True}}
    elif placement == "unsupported_arg":
        operation["arguments"][1] = {"type": "utf8", "value": "unadmitted pointer argument"}
        recipe["consumer"]["call"]["argument_types"][1] = "utf8"
        _refresh_contract_pins(case, recipes)
    else:
        inner = {"kind": "native_function", **{key: value for key, value in operation.items() if key != "op"}}
        operation = copy.deepcopy(operation)
        operation["arguments"][0] = {"type": "pointer", "value": {"getter": inner}}
        case["operations"] = [operation]
    output = _run(case)
    _refused(output, calls=0)
    assert output["rootCalls"] == output["functionsCreated"] == 0 and output["borrowedTrace"] == []


@pytest.mark.parametrize("forgery", ["signature", "signature_pointer", "signature_duplicate", "signature_extra",
                                   "contract_basis", "contract_extra", "source_role", "missing_original"])
def test_receiver_hypothesis_reconstructs_exact_unsigned_declaration_resources(forgery):
    case, recipes = _receiver_hypothesis_case()
    declaration = case["resources"]["/fixture/path.json"]
    if forgery == "signature":
        declaration["native_receiver_signatures"][1]["signature"]["return_type"] = "pointer"
    elif forgery == "signature_pointer":
        declaration["native_receiver_signatures"][0]["pointer"] = "/signatures/consumer"
    elif forgery == "signature_duplicate":
        declaration["native_receiver_signatures"].append(copy.deepcopy(declaration["native_receiver_signatures"][0]))
    elif forgery == "signature_extra":
        declaration["native_receiver_signatures"][0]["trusted"] = True
    elif forgery == "contract_basis":
        declaration["native_path_contracts"][0]["contract"]["evidence_basis"]["lifetime_proven"] = True
    elif forgery == "contract_extra":
        declaration["native_path_contracts"][0]["contract"]["concurrent_invalidation"] = "independently_excluded"
    elif forgery == "source_role":
        for wrapper in [recipes[0]["root_evidence"], recipes[0]["lifetime"]["evidence"],
                        recipes[0]["types"][0]["evidence"], recipes[0]["consumer"]["evidence"]]:
            wrapper["originals"][0]["role"] = "header"
        _refresh_contract_pins(case, recipes)
    else:
        del case["resources"]["/fixture/vptr_data.json"]
    output = _run(case)
    _refused(output, calls=0)
    assert output["rootCalls"] == output["functionsCreated"] == 0 and output["borrowedTrace"] == []


@pytest.mark.parametrize("attack", ["copied", "slot", "transaction", "reused", "function"])
def test_receiver_hypothesis_private_permits_refuse_forgery_and_expire_after_refusal(attack):
    case, _ = _receiver_hypothesis_case()
    injection = {
        "copied": "nativePathCalls.get(pathCall).permits[0] = Object.freeze({});",
        "slot": "nativePathPermits.get(nativePathCalls.get(pathCall).permits[0]).argument = 1;",
        "transaction": "nativePathPermits.get(nativePathCalls.get(pathCall).permits[0]).transaction = Object.freeze({});",
        "reused": "consumeNativePathPermits(pathCall, found.function_, arguments_);",
        "function": "return targetCall(() => 0, arguments_, pathCall);",
    }[attack]
    probe = "globalThis.pathProbe = () => consumeNativePathPermits(pathCall, found.function_, arguments_);"
    output = _run(case, "stale_permit", instrument=probe + injection)
    _refused(output, calls=1)
    assert output["rootCalls"] == 1 and "not current" in output["stale"]


@pytest.mark.parametrize("stage", ["raw_return", "getter_postprocess", "before_seal", "after_seal"])
@pytest.mark.parametrize("spoof", [False, True])
def test_receiver_hypothesis_private_epoch_rejects_intervening_calls_even_with_spoofed_counter(stage, spoof):
    case, _ = _receiver_hypothesis_case()
    case["poison_after_intervention"] = True
    output = _run(case, **_borrowed_epoch_fault(stage, spoof=spoof))
    _refused(output, calls=1 if spoof else 2)
    assert output["interventions"] == output["rootCalls"] == 1 and output["calls"] == [0x1400]
    assert all(read["after_intervention"] == 0 for read in output["borrowedTrace"])


def test_receiver_hypothesis_never_dereferences_a_receiver_after_its_consumer_returns():
    case, recipes = _receiver_hypothesis_case()
    output = _run(case, "receiver_discard_after_consumer")
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == 2 and result["invocations"] == 1
    assert output["discarded"] == output["consumed"] == [0x3000] and result["handles_expired"] is True
    assert [row["reader"] for row in output["borrowedTrace"]] == ["readPointer", "readPointer"]
    P.validate_observations(result["operations"][0]["native_paths"], recipes)


def test_borrowed_root_dispatch_is_one_use_and_saved_request_resolves_a_fresh_root():
    case, recipes = _borrowed_root_case()
    saved = copy.deepcopy(case)
    output = _run(case, repeat=True)
    assert case == saved and output["rootCalls"] == output["consumerCalls"] == 2
    assert output["mutatorCalls"] == output["retains"] == output["releases"] == 0
    assert output["consumed"] == [0x3000, 0x5000] and output["calls"] == [0x1400, 0x1440, 0x1400, 0x1440]
    for result in output["results"]:
        assert result["status"] == "completed" and result["native_calls"] == 2 and result["invocations"] == 1
        row = result["operations"][0]
        assert row["value"] == 64 and result["handles_expired"] and not result["native_state_uncertain"]
        assert P.validate_observations(row["native_paths"], recipes) == row["native_paths"]
        path = row["native_paths"][0]
        assert path["owner_retained"] is False and len(path["hops"]) == 1
        assert path["hops"][0]["edge"] is None and path["hops"][0]["identity_checked"] is True
        assert path["borrowed_scope"] == {"getter_epoch": 1, "checked_epoch": 1,
            "use": "dispatch", "consumed": True, "expires": "use"}


def test_borrowed_root_immediate_scalar_read_consumes_read_scope_without_call_permit():
    case, recipes = _borrowed_root_case(read=True)
    output = _run(case)
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == 1 and result["invocations"] == 0
    assert output["rootCalls"] == 1 and output["consumerCalls"] == output["mutatorCalls"] == 0
    row = result["operations"][0]
    assert [field["value"] for field in row["fields"]] == [7, 41]
    assert P.validate_observations(row["native_paths"], recipes, for_call=False) == row["native_paths"]
    path = row["native_paths"][0]
    assert path["sealed"] is path["dispatch_permit_consumed"] is path["owner_retained"] is False
    assert path["borrowed_scope"] == {"getter_epoch": 1, "checked_epoch": 1,
        "use": "read", "consumed": True, "expires": "use"}
    with pytest.raises(ValueError): P.validate_observations(row["native_paths"], recipes)
    assert result["handles_expired"] and output["retains"] == output["releases"] == 0


def test_borrowed_wrong_vptr_prefix_refuses_before_derived_extent_or_getter_reacquisition():
    case, _ = _borrowed_root_case()
    output = _run(case, "borrowed_wrong_prefix")
    result = _refused(output, calls=1)
    assert result["error"]["message"] == "native path pinned data identity differs from its exact non-instruction section"
    assert output["rootCalls"] == 1 and output["mutatorCalls"] == 0
    assert output["borrowedTrace"] == [{"address": 0x3000, "reader": "readPointer", "after_intervention": 0}]
    assert result["operations"] == []


def test_borrowed_lifetime_original_admission_is_checked_before_the_root_getter():
    case, _ = _borrowed_root_case()
    case["resources"]["/fixture/path.json"]["native_path_contracts"] = [
        row for row in case["resources"]["/fixture/path.json"]["native_path_contracts"] if row["pointer"] != "/lifetime"]
    output = _run(case)
    result = _refused(output, calls=0)
    assert "contract/original correlation" in result["error"]["message"]
    assert output["functionsCreated"] == output["rootCalls"] == 0 and output["borrowedTrace"] == []


@pytest.mark.parametrize("placement", ["nonterminal", "graph", "nested", "alias", "utf8", "scalar_getter"])
def test_fixed_runtime_rejects_borrowed_scope_misuse_before_any_root_getter(placement):
    case, recipes = _borrowed_root_case(read=placement == "graph")
    recipe, operation = recipes[0], case["operations"][0]
    if placement == "nonterminal":
        case["operations"].append({"op": "modules"})
    elif placement == "graph":
        case["operations"] = [{"op": "graph", "root": {"native_path": recipe}, "root_type": "Node",
            "types": [{"name": "Node", "bytes": 16, "fields": copy.deepcopy(recipe["types"][0]["fields"]),
                       "layout_evidence": operation["layout_evidence"]}], "max_depth": 0, "max_nodes": 1, "methods_limit": 0}]
    elif placement == "nested":
        inner = {"kind": "native_function", **{key: value for key, value in operation.items() if key != "op"}}
        outer = copy.deepcopy(operation)
        outer["arguments"][0] = {"type": "pointer", "value": {"getter": inner}}
        case["operations"] = [outer]
    elif placement == "alias": operation["alias"] = "escaped"
    elif placement == "utf8":
        operation["arguments"][1] = {"type": "utf8", "value": "unadmitted pointer argument"}
        recipe["consumer"]["call"]["argument_types"][1] = "utf8"
    else: operation["arguments"][1]["value"] = {"getter": copy.deepcopy(recipe["root"]["getter"])}
    # Adversarial payload enters the fixed JS directly, with exact current
    # declarations. This must not pass just because Python would reject it.
    _refresh_contract_pins(case, recipes)
    output = _run(case)
    result = _refused(output, calls=0)
    assert "borrowed root" in result["error"]["message"]
    assert output["rootCalls"] == output["mutatorCalls"] == 0 and output["borrowedTrace"] == []


def _borrowed_epoch_fault(stage, *, spoof):
    action = "targetCall(inertIntervention, []);" + (" result.native_calls = 1;" if spoof else "")
    if stage == "raw_return":
        needle = "const value = function_(...arguments_);"
        return {"source_copy": {needle: needle + "\n        if (scope !== null) { " + action + " }"}}
    if stage == "getter_postprocess":
        needle = "      const outBuffers = [];\n      let completion = null;"
        return {"source_copy": {needle: "      if (rootCapture !== null) { " + action + " }\n" + needle}}
    if stage == "before_seal":
        needle = "        if (pathCall !== null) sealNativePaths(pathCall);"
        return {"source_copy": {needle: "        if (pathCall !== null) { " + action + " }\n" + needle}}
    if stage == "after_seal":
        return {"instrument": action}
    assert stage == "between_read_fields"
    needle = "            value: serializeNative(value, field.type, field.alias), observation: 'declared_memory_read'});"
    injection = "\n          if (prepared !== null && prepared.recipe.lifetime.mode === 'main_queue_borrowed_root' && row.fields.length === 1) { " + action + " }"
    return {"source_copy": {needle: needle + injection}}


@pytest.mark.parametrize("stage", ["raw_return", "getter_postprocess", "before_seal", "after_seal", "between_read_fields"])
@pytest.mark.parametrize("spoof", [False, True])
def test_private_borrowed_epoch_refuses_intervening_calls_before_any_next_receiver_read_or_dispatch(stage, spoof):
    case, _ = _borrowed_root_case(read=stage == "between_read_fields")
    case["poison_after_intervention"] = True
    output = _run(case, **_borrowed_epoch_fault(stage, spoof=spoof))
    result = _refused(output, calls=1 if spoof else 2)
    assert "borrowed root epoch/operation expired or changed" in result["error"]["message"]
    assert output["interventions"] == output["rootCalls"] == 1
    assert output["calls"] == [0x1400] and output["mutatorCalls"] == 0 and result["operations"] == []
    assert all(read["after_intervention"] == 0 for read in output["borrowedTrace"])
    if stage in {"raw_return", "getter_postprocess"}:
        assert output["borrowedTrace"] == []
    if stage == "between_read_fields":
        assert [read["address"] for read in output["borrowedTrace"]] == [0x3000, 0x3008, 0x3008]
    # Fixture interventions are independent of the mutable public counter;
    # restoring that counter must not make the private lease usable again.
    assert output["consumerCalls"] == 0 and result["native_state_uncertain"] is False


def test_borrowed_read_selection_is_frozen_before_resolution_event_and_cannot_broaden():
    case, _ = _borrowed_root_case(read=True, scalar_fields=["generation"])
    output = _run(case, "borrowed_read_selection_drift")
    result = _refused(output, calls=1)
    assert "borrowed root epoch/operation expired or changed" in result["error"]["message"]
    assert output["rootCalls"] == 1 and output["borrowedTrace"] == [
        {"address": 0x3000, "reader": "readPointer", "after_intervention": 0},
        {"address": 0x3008, "reader": "readU32", "after_intervention": 0}]
    assert result["operations"] == []


def test_borrowed_dispatch_never_dereferences_the_receiver_after_the_consumer_returns():
    case, recipes = _borrowed_root_case()
    output = _run(case, "borrowed_discard_after_consumer")
    result = output["results"][0]
    assert result["status"] == "completed" and result["native_calls"] == 2 and result["invocations"] == 1
    assert result["operations"][0]["value"] == 64 and result["handles_expired"] and not result["native_state_uncertain"]
    assert output["discarded"] == output["consumed"] == [0x3000] and output["rootCalls"] == output["consumerCalls"] == 1
    assert [(read["reader"], read["address"]) for read in output["borrowedTrace"]] == [
        ("readPointer", 0x3000), ("readU32", 0x3008), ("readPointer", 0x3000), ("readU32", 0x3008), ("readS32", 0x300c)]
    P.validate_observations(result["operations"][0]["native_paths"], recipes)


def _checked_observer_result(output, recipes, *, read=False, generation=1):
    from anyuse.execution import rea_native_lifecycle as L
    result = output["results"][0]
    assert result["status"] == "completed" and result["error"] is None and result["handles_expired"] is True
    assert result["native_state_uncertain"] is False
    assert result["native_calls"] == result["invocations"] == int(not read)
    paths = result["operations"][0]["native_paths"]
    P.validate_observations(paths, recipes, for_call=not read)
    scope = paths[0]["lifecycle_scope"]
    observation = result["lifecycle_observer"]
    assert L.validate_result(observation, recipes[0]["root"]["lifecycle"], scope=scope, for_call=not read,
                            native_calls=result["native_calls"], invocations=result["invocations"]) == observation
    assert scope["generation"] == observation["selected_generation"] == generation
    assert scope["acquire_epoch"] == scope["checked_epoch"] == 0
    assert observation["hooks_requested"] == observation["hook_attempts"] == observation["hooks_installed"] == observation["hooks_detached"]
    assert observation["tokens_expired"] and observation["listeners_cleanup_flushed"]
    assert observation["callbacks_inflight"] == observation["lifecycle_frames_inflight"] == 0
    assert observation["events"][scope["acquire_event"] - 1]["kind"] == "acquire"
    assert scope["consume_event"] == scope["acquire_event"] + 1
    assert output["rootCalls"] == output["mutatorCalls"] == output["retains"] == output["releases"] == 0
    # The fixture exposes addresses for assertions; the product ledger never
    # exports a pointer, private Map key or reusable token.
    assert all(set(row) == {"seq", "kind", "generation", "entry", "return_site", "thread"} for row in observation["events"])
    assert not any(value in json.dumps(observation) for value in ("0x3000", "0x5000", "pointer", "token:"))
    return result


@pytest.mark.parametrize("read", [False, True])
def test_observed_root_uses_one_fresh_completed_birth_after_the_fixed_async_window_and_closes_own_hooks(read):
    case, recipes = _observed_root_case(read=read)
    output = _run(case)
    result = _checked_observer_result(output, recipes, read=read)
    assert [row["address"] for row in output["hookLog"] if row["kind"] == "attach"] == [0x1540, 0x1500]
    assert [row["address"] for row in output["hookLog"] if row["kind"] == "detach"] == [0x1500, 0x1540]
    assert [row["kind"] for row in result["lifecycle_observer"]["events"]] == [
        "armed", "constructor_enter", "constructor_return", "observation_end", "selected", "acquire", "consume", "stop"]
    if read:
        assert [field["value"] for field in result["operations"][0]["fields"]] == [7, 41]
        assert output["consumerCalls"] == output["functionsCreated"] == 0
    else:
        assert result["operations"][0]["value"] == 64 and output["consumerCalls"] == output["functionsCreated"] == 1


@pytest.mark.parametrize("scenario", ["lifecycle_none", "lifecycle_preexisting", "lifecycle_wrong_return",
    "lifecycle_two_live", "lifecycle_death", "lifecycle_same_vptr_death", "lifecycle_unwitnessed_reuse",
    "lifecycle_wrong_site_reuse", "lifecycle_wrong_thread", "lifecycle_wrong_thread_death",
    "lifecycle_selected_death", "lifecycle_selected_new_birth", "lifecycle_wrong_terminal_thread",
    "lifecycle_resource_drift", "lifecycle_code_base_drift", "lifecycle_expired_window", "lifecycle_event_before_armed",
    "lifecycle_unmatched_completion", "lifecycle_callback_argument_failure"])
def test_observer_never_adopts_existing_or_ambiguous_dead_wrong_owner_or_replacement_objects(scenario):
    case, _ = _observed_root_case()
    output = _run(case, scenario)
    result = _refused(output, calls=0)
    assert result["operations"] == [] and output["rootCalls"] == output["consumerCalls"] == 0
    observation = result["lifecycle_observer"]
    assert observation["status"] == "incomplete" and observation["tokens_expired"] is True
    assert observation["hooks_detached"] == observation["hooks_installed"] == observation["hooks_requested"] == 2
    assert observation["listeners_cleanup_flushed"] is True


@pytest.mark.parametrize("scenario", ["lifecycle_pending_constructor", "lifecycle_pending_destructor", "lifecycle_nested_constructor"])
def test_pending_lifecycle_frame_refuses_root_use_and_retains_phase_uncertainty_after_hook_cleanup(scenario):
    case, _ = _observed_root_case()
    output = _run(case, scenario)
    result = _refused(output, calls=0, uncertain=True)
    assert result["lifecycle_observer"]["lifecycle_frames_inflight"] == 1
    assert result["lifecycle_observer"]["hooks_detached"] == 2
    assert result["lifecycle_observer"]["status"] == "incomplete"


@pytest.mark.parametrize("read", [False, True])
def test_witnessed_destruction_and_fresh_complete_construction_issue_a_new_private_generation_even_at_same_address(read):
    case, recipes = _observed_root_case(read=read)
    output = _run(case, "lifecycle_witnessed_reuse")
    result = _checked_observer_result(output, recipes, read=read, generation=2)
    events = result["lifecycle_observer"]["events"]
    assert [row["generation"] for row in events if row["kind"] == "constructor_enter"] == [1, 2]
    assert [row["generation"] for row in events if row["kind"] == "destructor_enter"] == [1]


def test_nested_destructor_aliases_revoke_idempotently_without_manufacturing_generations():
    case, recipes = _observed_root_case(aliases=True)
    output = _run(case, "lifecycle_alias_death")
    result = _checked_observer_result(output, recipes, generation=2)
    events = result["lifecycle_observer"]["events"]
    assert [(row["entry"], row["generation"]) for row in events if row["kind"] == "destructor_enter"] == [(0, 1), (1, 1)]
    assert [(row["entry"], row["generation"]) for row in events if row["kind"] == "destructor_leave"] == [(1, 1), (0, 1)]


@pytest.mark.parametrize("kwargs", [{"event_limit": 8}, {"generation_limit": 1}])
def test_observer_budget_exhaustion_poison_is_retained_without_truncating_and_accepting_a_suffix(kwargs):
    case, _ = _observed_root_case(**kwargs)
    output = _run(case, "lifecycle_budget")
    result = _refused(output, calls=0)
    observation = result["lifecycle_observer"]
    assert observation["poisoned"] is True and len(observation["events"]) <= kwargs.get("event_limit", 64)
    assert [row["seq"] for row in observation["events"]] == list(range(1, len(observation["events"]) + 1))
    assert observation["events"][0]["kind"] == "armed" and observation["tokens_expired"]


def test_smallest_complete_ledger_budget_still_records_the_entire_sole_birth_and_use():
    case, recipes = _observed_root_case(event_limit=8)
    output = _run(case)
    result = _checked_observer_result(output, recipes)
    assert len(result["lifecycle_observer"]["events"]) == 8


@pytest.mark.parametrize("scenario,installed,detached", [
    ("lifecycle_install_failure", 1, 1), ("lifecycle_install_flush_failure", 2, 2),
    ("lifecycle_detach_failure", 2, 1), ("lifecycle_cleanup_flush_failure", 2, 2),
])
def test_checked_hook_failure_rolls_back_only_its_listeners_and_never_claims_clean_completion(scenario, installed, detached):
    case, _ = _observed_root_case()
    output = _run(case, scenario)
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["error"] and result["handles_expired"] is True
    observation = result["lifecycle_observer"]
    assert observation["status"] == "incomplete" and observation["tokens_expired"] is True
    assert observation["hooks_installed"] == installed and observation["hooks_detached"] == detached
    if scenario in {"lifecycle_install_failure", "lifecycle_install_flush_failure"}:
        assert output["consumerCalls"] == result["native_calls"] == 0
    assert all(row["address"] in {0x1500, 0x1540} for row in output["hookLog"] if "address" in row)


def test_observed_wrong_primary_vptr_refuses_before_reading_unmapped_derived_fields():
    case, _ = _observed_root_case()
    output = _run(case, "lifecycle_vptr_wrong_prefix")
    result = _refused(output, calls=0)
    assert "pinned data identity differs" in result["error"]["message"]
    assert [(row["kind"], row.get("reader")) for row in output["memoryTrace"] if 0x3000 <= row["address"] < 0x3010] == [
        ("range", None), ("read", "readPointer")]


def test_observation_budget_must_fit_the_original_deadline_without_extension_or_hook_installation():
    case, _ = _observed_root_case()
    case["deadline_ms"] = 4999  # now1000 + observe1000 + reserve3000 needs5000.
    output = _run(case)
    result = _refused(output, calls=0)
    assert "does not fit" in result["error"]["message"] and output["hookLog"] == []


def test_observing_async_rpc_reentry_is_refused_while_the_first_private_observer_remains_usable():
    case, recipes = _observed_root_case()
    output = _run(case, "lifecycle_rpc_reentry")
    _checked_observer_result(output, recipes)
    assert "borrowed native path lifetime" in output["reentrant"]


def test_observed_consumer_may_destroy_its_receiver_after_consume_without_any_post_dispatch_access():
    case, recipes = _observed_root_case()
    output = _run(case, "lifecycle_discard_after_consumer")
    result = _checked_observer_result(output, recipes)
    assert output["discarded"] == output["consumed"] == [0x3000]
    events = result["lifecycle_observer"]["events"]
    consume = next(index for index, row in enumerate(events) if row["kind"] == "consume")
    assert [row["kind"] for row in events[consume + 1:]] == ["destructor_enter", "destructor_leave", "stop"]


def test_observed_native_fault_keeps_the_consumed_call_and_uncertainty_despite_confirmed_hook_teardown():
    case, _ = _observed_root_case()
    output = _run(case, "consumer_fault")
    result = output["results"][0]
    assert result["status"] == "incomplete" and result["native_state_uncertain"] is True
    assert result["native_calls"] == result["invocations"] == output["consumerCalls"] == 1
    assert result["operations"] == [] and "inert target fault" in result["error"]["message"]
    observation = result["lifecycle_observer"]
    assert observation["status"] == "incomplete" and observation["hooks_detached"] == observation["hooks_installed"] == 2
    assert observation["tokens_expired"] and observation["listeners_cleanup_flushed"]
    assert sum(row["kind"] == "consume" for row in observation["events"]) == 1


def test_repeat_reobserves_a_fresh_birth_in_a_new_rpc_instead_of_reusing_a_persisted_slot():
    case, recipes = _observed_root_case()
    output = _run(case, repeat=True)
    for result in output["results"]:
        _checked_observer_result({**output, "results": [result], "consumerCalls": 1}, recipes)
    assert output["consumed"] == [0x3000, 0x5000] and output["consumerCalls"] == 2
    assert [result["operations"][0]["native_paths"][0]["lifecycle_scope"]["generation"] for result in output["results"]] == [1, 1]


@pytest.mark.parametrize("stage", ["before_acquire", "before_seal", "after_seal", "between_read_fields"])
@pytest.mark.parametrize("spoof", [False, True])
def test_observed_private_epoch_rejects_intervening_work_even_if_the_public_counter_is_restored(stage, spoof):
    case, _ = _observed_root_case(read=stage == "between_read_fields")
    case["poison_after_intervention"] = True
    action = "targetCall(inertIntervention, []);" + (" result.native_calls = 0;" if spoof else "")
    if stage == "before_acquire":
        needle = "        pointer = observer.selected.pointer;"
        options = {"source_copy": {needle: action + "\n" + needle}}
    elif stage == "before_seal":
        needle = "        if (pathCall !== null) sealNativePaths(pathCall);"
        options = {"source_copy": {needle: "        if (pathCall !== null) { " + action + " }\n" + needle}}
    elif stage == "after_seal":
        options = {"instrument": action}
    else:
        needle = "            value: serializeNative(value, field.type, field.alias), observation: 'declared_memory_read'});"
        options = {"source_copy": {needle: needle + "\n          if (prepared !== null && row.fields.length === 1) { " + action + " }"}}
    output = _run(case, **options)
    result = _refused(output, calls=0 if spoof else 1)
    assert "generation/owner/phase/epoch expired" in result["error"]["message"]
    assert output["interventions"] == 1 and output["rootCalls"] == output["consumerCalls"] == 0
    assert all(row["after_intervention"] == 0 for row in output["borrowedTrace"])
    assert result["lifecycle_observer"]["status"] == "incomplete" and result["lifecycle_observer"]["tokens_expired"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
