/* Fixed guest adapter. Compile with frida-compile + ObjC/Swift bridges in the VM.
 * Requests contain data only. No eval, method replacement, arbitrary pointers,
 * arbitrary file IO or protection changes are exposed.
 */
import ObjC from 'frida-objc-bridge';

const MAX_ROWS = 128;
const MAX_FIELDS = 32;
const MAX_STRING_UNITS = 4096;
const RECEIVER_HYPOTHESIS_FORMAT = 'anyuse.rea-native-receiver-hypothesis/1';
const RECEIVER_HYPOTHESIS_MODE = 'main_queue_binary_runtime_hypothesis';
const RECEIVER_DECLARATION_KIND = 'declared_native_receiver';
const RECEIVER_HYPOTHESIS_BASIS = Object.freeze({abi_basis: 'model_declared',
  receiver_basis: 'binary_correlated_hypothesis', lifetime_basis: 'assumed_not_established',
  abi_recovered: false, type_proven: false, lifetime_proven: false, proof_created: false});
const RECEIVER_HYPOTHESIS_V2_FORMAT = 'anyuse.rea-native-receiver-hypothesis/2';
const RECEIVER_HYPOTHESIS_V2_MODE = 'main_queue_binary_runtime_hypothesis_v2';
const RECEIVER_DECLARATION_V2_KIND = 'declared_native_receiver_v2';
const RECEIVER_HYPOTHESIS_V2_BASIS = Object.freeze({abi_basis: 'model_declared',
  receiver_basis: 'binary_correlated_hypothesis_v2', lifetime_basis: 'assumed_not_established',
  abi_recovered: false, type_proven: false, lifetime_proven: false, proof_created: false});
// A Swift SDK call is synchronous, but a cooperative native call may permit
// another RPC to enter this script. Never share its allocator capture scope.
let sdkAllocationCaptureOwner = null;
let nativePathQueueOwner = null;
const TYPE_CODES = {
  void: ['v'], bool: ['B', 'c'], int8: ['c'], uint8: ['C'], int16: ['s'], uint16: ['S'],
  int32: ['i'], uint32: ['I'], int64: ['q'], uint64: ['Q'], long: ['l'], ulong: ['L'],
  float: ['f'], double: ['d'], utf8: ['@'], object: ['@'], nil: ['@']
};

function fail(message) { throw new Error(message); }

// A separate observation path: no ObjC availability access, app dispatch,
// explicit adapter-side Mach-O header reads, getters, hooks or helper loading.
// SDK metadata APIs may inspect headers internally; their reads are not counted
// as the requested logical volatile reads. The static ObjC bridge import above
// remains part of the adapter; this does not claim zero library init.
function inspectMemory(payload) {
  const aliases = new Map();
  const result = {status: 'incomplete', operations: [], native_calls: 0, invocations: 0,
    helper_load_attempts: 0, callable_resolutions: 0, helper_residency: [], error: null,
    handles_expired: false, queue: 'runtime_inspection', native_state_uncertain: false,
    process: {pid: Process.id, executable: Process.mainModule.path, architecture: Process.arch}};
  const journal = result.memory_inspection = {
    format: 'anyuse.rea-memory-inspection/2', pointer_size: 8, byte_order: 'little',
    loaded_binding: 'path_metadata_only', loaded_image_verified: false,
    raw_observation_only: true, atomic_snapshot: false, verified: false,
    proof_created: false, signature_inferred: false, type_admitted: false, lifetime_admitted: false,
    range_inventory_materialization_bounded: false,
    metadata_reserved: 0, read_bytes_reserved: 0, charged_bytes: 0,
    known_returned_bytes: 0, actual_read_bytes: 0, aliases_expired: false,
    mappings: [], attempts: []};
  const kinds = new Map(), knownSpans = new Map(), fileClaims = new Map();
  const operations = payload.request && payload.request.operations;
  const names = new Set(['memory_ranges', 'memory_range', 'memory_bytes', 'memory_pointer_range']);
  function memoryError(error) {
    const value = error === null || error === undefined ? {name: 'Error', message: String(error)} : Object(error);
    const row = errorInfo(value);
    row.type = String(row.type).slice(0, 128) || 'Error';
    return row;
  }
  function require_(value, message) { if (!value) fail(message); }
  function bounded() { require_(Number.isFinite(payload.deadline_ms) && Date.now() < payload.deadline_ms, 'memory inspection deadline expired'); }
  function exact(value, keys) {
    require_(value !== null && typeof value === 'object' && !Array.isArray(value) &&
      dataEqual(Object.keys(value).sort(), keys.slice().sort()), 'memory inspection requires closed object keys');
  }
  function integer(value, minimum, maximum) {
    require_(Number.isSafeInteger(value) && value >= minimum && value <= maximum, 'memory inspection integer exceeds its bound');
    return value;
  }
  function fileClaim(value) {
    exact(value, ['path', 'sha256']);
    require_(typeof value.path === 'string' && value.path.length > 0 && value.path.length <= 4096 &&
      value.path.startsWith('/') && !value.path.includes('\0') &&
      value.path.split('/').slice(1).every(part => part !== '' && part !== '.' && part !== '..') &&
      typeof value.sha256 === 'string' && /^[0-9a-f]{64}$/.test(value.sha256), 'bounded exact module file claim required');
  }
  function aliasName(value) {
    require_(typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value) && !kinds.has(value), 'unique bounded memory alias required');
  }
  function requestFile(value) {
    fileClaim(value);
    require_(!fileClaims.has(value.path) || fileClaims.get(value.path) === value.sha256,
      'memory batch has conflicting file hashes for one module path');
    fileClaims.set(value.path, value.sha256);
  }
  function sourceShape(value, width) {
    require_(value && typeof value === 'object' && !Array.isArray(value), 'earlier typed memory source required');
    if (Object.prototype.hasOwnProperty.call(value, 'span')) {
      exact(value, ['span']);
      require_(kinds.get(value.span) === 'span', 'memory span alias is missing, forward or expired');
      return knownSpans.get(value.span);
    }
    exact(value, ['ranges', 'index']);
    require_(kinds.get(value.ranges) === 'ranges', 'memory ranges alias is missing, forward or expired');
    integer(value.index, 0, knownSpans.get(value.ranges) - 1);
    return null;
  }
  function pointerHex(value) {
    const text = value.toString();
    require_(/^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(text), 'canonical native pointer metadata required');
    return text;
  }
  function add(pointer, bytes) {
    integer(bytes, 0, Number.MAX_SAFE_INTEGER);
    const end = pointer.add(bytes);
    require_(end.compare(pointer) >= 0 && (bytes === 0 || end.compare(pointer) > 0), 'memory address arithmetic wrapped');
    return end;
  }
  function descriptor(range) {
    require_(range && range.base && Number.isSafeInteger(range.size) && range.size > 0 &&
      typeof range.protection === 'string' && /^[r-][w-][x-]$/.test(range.protection) &&
      range.protection[0] === 'r', 'one readable bounded mapping required');
    const base = pointerHex(range.base);
    add(range.base, range.size);
    let file = null;
    if (range.file !== undefined && range.file !== null) {
      const value = range.file;
      require_(typeof value.path === 'string' && value.path.startsWith('/') && value.path.length <= 4096 && !value.path.includes('\0'),
        'bounded mapping file path required');
      integer(value.offset, 0, Number.MAX_SAFE_INTEGER);
      const size = value.size === undefined ? null : integer(value.size, 0, Number.MAX_SAFE_INTEGER);
      file = {path: value.path, offset: value.offset, size};
    }
    return {base, bytes: range.size, protection: range.protection, file};
  }
  function appendMapping(range, error = null) {
    require_(journal.mappings.length < 128, 'memory mapping observation budget exhausted');
    const row = {at_ms: Date.now(), status: 'unmapped', base: null, bytes: null, protection: null, file: null, error: null};
    journal.mappings.push(row);
    if (error !== null) { row.status = 'error'; row.error = memoryError(error); }
    else if (range !== null) {
      try { Object.assign(row, descriptor(range), {status: 'mapped'}); }
      catch (failure) { row.status = 'error'; row.error = memoryError(failure); }
    }
    return journal.mappings.length - 1;
  }
  function mappingAt(pointer) {
    bounded();
    try { return appendMapping(Process.findRangeByAddress(pointer)); }
    catch (error) { return appendMapping(null, error); }
  }
  function mappingData(index) {
    const row = journal.mappings[index];
    require_(row && row.status === 'mapped', 'current readable memory mapping unavailable');
    return {base: row.base, bytes: row.bytes, protection: row.protection, file: row.file};
  }
  function inside(pointer, bytes, mapping) {
    const end = add(pointer, bytes), first = ptr(mapping.base), last = add(first, mapping.bytes);
    require_(pointer.compare(first) >= 0 && end.compare(last) <= 0, 'requested span exceeds one readable mapping');
  }
  function moduleNow(claim) {
    bounded(); fileClaim(claim);
    const pin = (payload.native_resources || {})[claim.path];
    require_(pin && pin.file === claim.path && pin.sha256 === claim.sha256, 'memory scope module lacks independently pinned file provenance');
    const modules = Process.enumerateModules().filter(module => module.path === claim.path);
    require_(modules.length === 1, 'one exact current module path required for memory scope');
    const module = modules[0];
    integer(module.size, 1, 512 * 1024 * 1024);
    const base = pointerHex(module.base); add(module.base, module.size);
    require_(typeof module.enumerateRanges === 'function', 'current module readable-range API unavailable; no fallback');
    // Module.size is reported metadata, not authority over data/zero-fill ranges.
    // Only this live module API supplies module-attributed coverage metadata. Its
    // complete native array is materialized before this retained-entry cap.
    const liveRanges = module.enumerateRanges('r--');
    require_(Array.isArray(liveRanges) && liveRanges.length >= 1 && liveRanges.length <= 32,
      'current module requires1..32 complete live readable ranges');
    const maximum = add(module.base, 512 * 1024 * 1024);
    const ranges = liveRanges.map(range => descriptor(range)).sort((left, right) => ptr(left.base).compare(ptr(right.base)));
    for (let index = 0; index < ranges.length; index++) {
      const first = ptr(ranges[index].base), end = add(first, ranges[index].bytes);
      require_(first.compare(module.base) >= 0 && end.compare(maximum) <= 0,
        'current module readable range exceeds its explicit bounded coverage');
      if (index > 0) require_(first.compare(add(ptr(ranges[index - 1].base), ranges[index - 1].bytes)) >= 0,
        'current module readable ranges overlap');
    }
    return {path: module.path, base, bytes: module.size, file: {path: claim.path, sha256: claim.sha256},
      loaded_binding: 'path_metadata_only', loaded_image_verified: false, ranges};
  }
  function moduleContains(module, address, bytes) {
    const end = add(address, bytes);
    require_(module.ranges.filter(range => address.compare(ptr(range.base)) >= 0 &&
      end.compare(add(ptr(range.base), range.bytes)) <= 0).length === 1,
      'requested span must fit exactly one current module readable range');
  }
  function issueError(attempt, stage, error) { attempt.errors.push({stage, error: memoryError(error)}); }
  function recordedFailure() { const error = new Error('memory observation incomplete; retained actual attempt'); error.memoryRecorded = true; throw error; }
  function spanRecord(span) { return {base: pointerHex(span.base), bytes: span.bytes, mapping_index: span.mappingIndex}; }
  function sourceOf(value) {
    let span;
    if (Object.prototype.hasOwnProperty.call(value, 'span')) {
      const entry = aliases.get(value.span);
      require_(entry && entry.kind === 'span', 'memory span alias expired or unavailable'); span = entry.span;
    } else {
      const entry = aliases.get(value.ranges);
      require_(entry && entry.kind === 'ranges', 'memory range-list alias expired or unavailable');
      integer(value.index, 0, entry.spans.length - 1); span = entry.spans[value.index];
    }
    return span;
  }
  function readSpan(attempt, span, offset, bytes) {
    attempt.stage = 'source';
    integer(offset, 0, span.bytes); integer(bytes, 1, 16384);
    require_(bytes <= span.bytes - offset, 'requested read exceeds its selected alias span');
    const address = add(span.base, offset); add(address, bytes);
    require_(typeof address.readVolatile === 'function', 'bounded volatile reader unavailable; no fallback');
    attempt.stage = 'mapping_before';
    attempt.mapping_before = mappingAt(address);
    const before = mappingData(attempt.mapping_before), original = mappingData(span.mappingIndex);
    inside(address, bytes, before);
    require_(dataEqual(before, original), 'memory alias mapping changed before reading');
    let raw = null;
    try {
      attempt.stage = 'charge'; bounded();
      require_(journal.charged_bytes + bytes <= 65536, 'memory read byte budget exhausted');
      const reading = attempt.read = {offset, requested_bytes: bytes, issued: true, returned: false,
        bytes_hex: null, returned_bytes: null, unknown_partial: false};
      journal.charged_bytes += bytes; // Charge immediately BEFORE the sole volatile read.
      attempt.stage = 'read';
      try {
        raw = address.readVolatile(bytes);
        if (!(raw instanceof ArrayBuffer) || raw.byteLength > bytes) {
          reading.unknown_partial = true; journal.actual_read_bytes = null;
          fail('volatile reader returned no bounded observable buffer');
        }
        const observed = new Uint8Array(raw);
        reading.returned = true; reading.returned_bytes = observed.length;
        reading.bytes_hex = Array.from(observed, byte => byte.toString(16).padStart(2, '0')).join('');
        journal.known_returned_bytes += observed.length;
        if (observed.length !== bytes) {
          reading.unknown_partial = true; journal.actual_read_bytes = null;
          fail('volatile reader returned a short buffer; no retry');
        }
        if (journal.actual_read_bytes !== null) journal.actual_read_bytes = journal.known_returned_bytes;
      } catch (error) {
        if (!reading.returned) { reading.unknown_partial = true; journal.actual_read_bytes = null; }
        throw error;
      }
    } catch (error) { issueError(attempt, attempt.stage, error); }
    // Metadata audits are independent observations, never a retry of the read.
    try {
      attempt.stage = 'mapping_after'; attempt.mapping_after = mappingAt(address);
      const after = mappingData(attempt.mapping_after); inside(address, bytes, after);
      require_(dataEqual(before, after), 'memory mapping changed after reading; raw bytes remain observational');
    } catch (error) { issueError(attempt, 'mapping_after', error); }
    if (attempt.errors.length) recordedFailure();
    return new Uint8Array(raw);
  }
  try {
    bounded();
    require_(Process.platform === 'darwin' && ['arm64', 'x64'].includes(Process.arch) && Process.pointerSize === 8,
      'raw memory inspection supports current little-endian Darwin arm64/x64 pointerSize8 only');
    require_(Process.id === payload.pid && Process.mainModule.path === payload.executable, 'memory inspection target process differs');
    exact(payload.request, ['operations']);
    require_(Array.isArray(operations) && operations.length >= 1 && operations.length <= 16, 'memory inspection needs1..16 operations');
    for (const operation of operations) {
      require_(operation && names.has(operation.op), 'memory inspection batch cannot mix calls or other operations');
      if (operation.op === 'memory_ranges') {
        exact(operation, ['op', 'scope', 'limit', 'as']); aliasName(operation.as);
        if (operation.scope && operation.scope.kind === 'all') exact(operation.scope, ['kind']);
        else { exact(operation.scope, ['kind', 'module']); require_(operation.scope.kind === 'module', 'literal memory scope required'); requestFile(operation.scope.module); }
        integer(operation.limit, 1, 128); journal.metadata_reserved += operation.limit;
        kinds.set(operation.as, 'ranges'); knownSpans.set(operation.as, operation.limit);
      } else if (operation.op === 'memory_range') {
        exact(operation, ['op', 'module', 'rva', 'bytes', 'as']); aliasName(operation.as); requestFile(operation.module);
        require_(typeof operation.rva === 'string' && /^0x(?:0|[1-9a-f][0-9a-f]{0,7})$/.test(operation.rva), 'canonical bounded module RVA required');
        integer(operation.bytes, 1, 16384); integer(Number(operation.rva), 0, 512 * 1024 * 1024 - operation.bytes);
        journal.metadata_reserved++; kinds.set(operation.as, 'span'); knownSpans.set(operation.as, operation.bytes);
      } else {
        const pointer = operation.op === 'memory_pointer_range';
        exact(operation, pointer ? ['op', 'from', 'offset', 'pointee_bytes', 'as'] : ['op', 'from', 'offset', 'bytes']);
        const bytes = pointer ? 8 : integer(operation.bytes, 1, 16384);
        const extent = sourceShape(operation.from, bytes); integer(operation.offset, 0, Number.MAX_SAFE_INTEGER - bytes);
        require_(extent === null || operation.offset <= extent && bytes <= extent - operation.offset, 'memory read exceeds prior selected span');
        journal.read_bytes_reserved += bytes; journal.metadata_reserved += pointer ? 3 : 2;
        if (pointer) { integer(operation.pointee_bytes, 1, 16384); aliasName(operation.as); kinds.set(operation.as, 'span'); knownSpans.set(operation.as, operation.pointee_bytes); }
      }
    }
    require_(journal.metadata_reserved <= 128 && journal.read_bytes_reserved <= 65536, 'memory inspection whole-batch budget exceeded');
    for (let index = 0; index < operations.length; index++) {
      const operation = operations[index];
      const attempt = {index, op: operation.op, status: 'incomplete', stage: 'validate', started_ms: Date.now(), finished_ms: null,
        errors: [], module_before: null, module_after: null, selections: [], source: null,
        mapping_before: null, mapping_after: null, pointee_mapping: null, read: null, matched: null, truncated: null};
      journal.attempts.push(attempt);
      let moduleClaim = null, originalModule = null, pending = null;
      try {
        bounded();
        let source = null;
        if (operation.op === 'memory_range') moduleClaim = operation.module;
        else if (operation.op === 'memory_ranges' && operation.scope.kind === 'module') moduleClaim = operation.scope.module;
        else if (operation.op === 'memory_bytes' || operation.op === 'memory_pointer_range') {
          attempt.stage = 'source'; source = sourceOf(operation.from); attempt.source = spanRecord(source);
          moduleClaim = source.moduleClaim; originalModule = source.moduleStamp;
        }
        if (moduleClaim !== null) {
          attempt.stage = 'module_before'; attempt.module_before = moduleNow(moduleClaim);
          require_(originalModule === null || dataEqual(originalModule, attempt.module_before), 'memory alias module path metadata changed');
        }
        const module = attempt.module_before;
        if (operation.op === 'memory_ranges') {
          attempt.stage = 'ranges';
          // Frida materializes the complete native array. Only RETAINED metadata
          // is capped; this makes no pre-materialization allocation-cap claim.
          const ranges = Process.enumerateRanges({protection: 'r--', coalesce: false});
          require_(Array.isArray(ranges), 'memory range inventory unavailable');
          const spans = []; let matched = 0;
          for (const range of ranges) {
            bounded(); const mapping = descriptor(range);
            const first = ptr(mapping.base), last = add(first, mapping.bytes);
            for (const coverage of module === null ? [mapping] : module.ranges) {
              const start = ptr(coverage.base), end = add(start, coverage.bytes);
              const clippedFirst = first.compare(start) < 0 ? start : first;
              const clippedLast = last.compare(end) > 0 ? end : last;
              if (clippedLast.compare(clippedFirst) <= 0) continue;
              matched++;
              if (spans.length >= operation.limit) continue;
              const bytes = Number(clippedLast.sub(clippedFirst).toString());
              integer(bytes, 1, Number.MAX_SAFE_INTEGER);
              if (module !== null) moduleContains(module, clippedFirst, bytes);
              const mappingIndex = appendMapping(range);
              const span = {base: clippedFirst, bytes, mappingIndex, moduleClaim, moduleStamp: module};
              spans.push(span); attempt.selections.push(spanRecord(span));
            }
          }
          attempt.matched = matched; attempt.truncated = matched > spans.length;
          pending = {kind: 'ranges', spans};
        } else if (operation.op === 'memory_range') {
          attempt.stage = 'select';
          const address = add(ptr(module.base), Number(operation.rva));
          moduleContains(module, address, operation.bytes);
          const mappingIndex = mappingAt(address);
          const span = {base: address, bytes: operation.bytes, mappingIndex, moduleClaim, moduleStamp: module};
          attempt.mapping_before = mappingIndex;
          inside(address, operation.bytes, mappingData(mappingIndex)); attempt.selections.push(spanRecord(span));
          pending = {kind: 'span', span};
        } else {
          const observed = readSpan(attempt, source, operation.offset, operation.op === 'memory_pointer_range' ? 8 : operation.bytes);
          if (operation.op === 'memory_pointer_range') {
            attempt.stage = 'pointee';
            const bits = Array.from(observed).reverse().map(byte => byte.toString(16).padStart(2, '0')).join('').replace(/^0+/, '') || '0';
            const destination = ptr('0x' + bits); // Exact retained bits; no tag/PAC stripping or guessing.
            require_(!destination.isNull(), 'observed pointer bits are null; no destination alias');
            attempt.pointee_mapping = mappingAt(destination);
            inside(destination, operation.pointee_bytes, mappingData(attempt.pointee_mapping));
            const span = {base: destination, bytes: operation.pointee_bytes, mappingIndex: attempt.pointee_mapping, moduleClaim: null, moduleStamp: null};
            attempt.selections.push(spanRecord(span)); pending = {kind: 'span', span};
          }
        }
      } catch (error) { if (!error || !error.memoryRecorded) issueError(attempt, attempt.stage, error); }
      // Recheck the module independently even when an issued read/pointee failed.
      if (attempt.module_before !== null) {
        try {
          attempt.stage = 'module_after'; attempt.module_after = moduleNow(moduleClaim);
          require_(dataEqual(attempt.module_before, attempt.module_after), 'memory scope module metadata changed after observation');
        } catch (error) { issueError(attempt, 'module_after', error); }
      }
      try { attempt.stage = 'deadline'; bounded(); }
      catch (error) { issueError(attempt, 'deadline', error); }
      attempt.finished_ms = Date.now();
      if (attempt.errors.length) {
        attempt.stage = attempt.errors[0].stage; result.error = attempt.errors[0].error; break;
      }
      if (pending !== null) aliases.set(operation.as, pending);
      attempt.stage = 'complete'; attempt.status = 'observed';
      result.operations.push({index, op: operation.op, status: 'ok', inspection_index: index});
    }
    if (result.operations.length === operations.length && result.error === null) result.status = 'completed';
  } catch (error) { result.error = memoryError(error); }
  finally {
    aliases.clear(); kinds.clear(); knownSpans.clear(); fileClaims.clear(); journal.aliases_expired = true; result.handles_expired = true;
  }
  return result;
}

function dataEqual(left, right) {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object' || Array.isArray(left) !== Array.isArray(right)) return false;
  const leftKeys = Object.keys(left).sort(), rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && dataEqual(left[key], right[key]));
}

function errorInfo(error) {
  return {type: error.name || 'Error', message: String(error.message || error).slice(0, 4096),
    stack: typeof error.stack === 'string' ? error.stack.slice(0, 8192) : null};
}

function nativeArgumentNeedsSwift(argument) {
  if (!argument || !argument.type) return false;
  const type = argument.type, value = argument.value;
  if (typeof type === 'object') {
    if (type.kind === 'swift_type') return true;
    if (type.kind === 'struct') return type.fields.some(field =>
      nativeArgumentNeedsSwift({type: field.type, value: value && value[field.name]}));
  }
  if (type !== 'pointer' || !value) return false;
  if (value.native_path !== undefined) return nativePathNeedsSwift(value.native_path);
  if (value.swift_read !== undefined) return true;
  if (value.getter !== undefined) return nativeCallNeedsSwift(value.getter);
  // A callable resolves an original declaration without constructing its ABI.
  // In particular, a C compiler helper can dispatch Swift without this bridge.
  return false;
}

function nativePathNeedsSwift(recipe) {
  return recipe.root.swift_read !== undefined ||
    (recipe.root.getter !== undefined && nativeCallNeedsSwift(recipe.root.getter));
}

function containsNativePath(value) {
  return value !== null && typeof value === 'object' &&
    (value.native_path !== null && typeof value.native_path === 'object' &&
      ['anyuse.rea-native-pointer-path/1', RECEIVER_HYPOTHESIS_FORMAT, RECEIVER_HYPOTHESIS_V2_FORMAT].includes(value.native_path.format) ||
      Object.values(value).some(containsNativePath));
}

function containsReceiverDeclaration(value) {
  return value !== null && typeof value === 'object' &&
    (value.format === RECEIVER_HYPOTHESIS_FORMAT || value.mode === RECEIVER_HYPOTHESIS_MODE ||
      value.kind === RECEIVER_DECLARATION_KIND ||
      value.format === RECEIVER_HYPOTHESIS_V2_FORMAT || value.mode === RECEIVER_HYPOTHESIS_V2_MODE ||
      value.kind === RECEIVER_DECLARATION_V2_KIND ||
      Object.values(value).some(containsReceiverDeclaration));
}

function nativeCallNeedsSwift(call) {
  return call.abi === 'swift' ||
    (typeof call.return_type === 'object' && call.return_type.kind === 'swift_type') ||
    (call.arguments || []).some(nativeArgumentNeedsSwift) ||
    (call.context !== undefined && nativeArgumentNeedsSwift(call.context));
}

function requestNeedsSwift(request) {
  return request.operations.some(operation => {
    if (['swift_types', 'swift_fields', 'swift_read'].includes(operation.op)) return true;
    if (operation.op === 'native_invoke') return nativeCallNeedsSwift(operation);
    if (operation.op === 'native_read') return operation.receiver.native_path !== undefined
      ? nativePathNeedsSwift(operation.receiver.native_path)
      : operation.receiver.native !== undefined && nativeCallNeedsSwift(operation.receiver.native);
    if (operation.op === 'graph') return operation.root.native_path !== undefined
      ? nativePathNeedsSwift(operation.root.native_path)
      : operation.root.native !== undefined && nativeCallNeedsSwift(operation.root.native);
    return false;
  });
}

function tokens(signature) {
  const result = [];
  let index = 0;
  while (index < signature.length) {
    while (index < signature.length && /[0-9rnNoORV]/.test(signature[index])) index++;
    if (index === signature.length) break;
    const token = signature[index++];
    if (!'vBcCsSiIlLqQfd@:#*'.includes(token)) fail('unsupported runtime ABI');
    if (token === '@' && signature[index] === '?') fail('runtime blocks are unsupported');
    if (token === '@' && signature[index] === '"') {
      const end = signature.indexOf('"', index + 1);
      if (end === -1) fail('incomplete object encoding');
      index = end + 1;
    }
    result.push(token);
  }
  return result;
}

function compatible(type, token) { return (TYPE_CODES[type] || []).includes(token); }

function classNamed(name) {
  const value = ObjC.classes[name];
  if (value === undefined || value.$kind !== 'class') fail('exact Objective-C class is unavailable: ' + name);
  return value;
}

function classModule(object) {
  const klass = object.$kind === 'class' ? object : object.$class;
  const image = ObjC.api.class_getImageName(klass.handle);
  return image.isNull() ? null : image.readUtf8String();
}

function methodOrigin(owner) {
  return owner === null ? 'runtime_generated' : 'module_backed';
}

function methodOf(receiver, selector, signature, arguments_, returnType) {
  const prefix = receiver.$kind === 'class' ? '+ ' : '- ';
  const key = prefix + selector;
  // Lookup only real listed methods. Wrapper misses can forward selectors.
  if (!receiver.$methods.includes(key)) fail('exact runtime selector is not listed: ' + key);
  const method = receiver[key];
  if (typeof method !== 'function' || typeof method.types !== 'string') fail('runtime method has no signature');
  if (signature !== undefined && method.types !== signature) fail('runtime method signature differs from frozen evidence');
  const parts = tokens(method.types);
  if (parts.length !== arguments_.length + 3 || parts[1] !== '@' || parts[2] !== ':' ||
      !compatible(returnType, parts[0]) || selector.split(':').length - 1 !== arguments_.length)
    fail('runtime method ABI does not fit declared return/arguments');
  arguments_.forEach((arg, index) => {
    if (!compatible(arg.type, parts[index + 3])) fail('runtime argument type mismatch');
  });
  const owner = Process.findModuleByAddress(method.implementation);
  return { method, metadata: {
    selector, dispatch: prefix[0], signature: method.types,
    class: receiver.$className, module: owner ? owner.path : null,
    implementation_offset: owner ? method.implementation.sub(owner.base).toString() : null,
    origin: methodOrigin(owner),
    return_type: returnType, argument_types: arguments_.map(arg => arg.type)
  }};
}

function ivarsFor(klass, own, query, limit) {
  const rows = [];
  const lowered = query.toLowerCase();
  let matched = 0, visited = 0, current = klass;
  while (current !== null && visited++ < 64) {
    const countPointer = Memory.alloc(4);
    countPointer.writeU32(0);
    const items = ObjC.api.class_copyIvarList(current.handle, countPointer);
    try {
      const count = countPointer.readU32();
      if (count > 8192) fail('ivar metadata exceeds the scan budget');
      for (let index = 0; index < count; index++) {
        const item = items.add(index * Process.pointerSize).readPointer();
        const name = ObjC.api.ivar_getName(item).readUtf8String();
        const encoding = ObjC.api.ivar_getTypeEncoding(item).readUtf8String();
        if (!name.toLowerCase().includes(lowered)) continue;
        matched++;
        if (rows.length < limit) rows.push({name, encoding, class: current.$className,
          module: classModule(current), offset: ObjC.api.ivar_getOffset(item).toString()});
      }
    } finally {
      if (!items.isNull()) ObjC.api.free(items);
    }
    if (own) break;
    current = current.$superClass;
  }
  return {rows, matched, truncated: matched > rows.length};
}

function ivarEncoding(receiver, name) {
  const rows = ivarsFor(receiver.$class, false, name, MAX_ROWS).rows.filter(row => row.name === name);
  if (rows.length !== 1) fail('need one exact declared ivar with a runtime encoding');
  return rows[0];
}

rpc.exports = {
  async run(payload) {
    if (sdkAllocationCaptureOwner !== null) fail('runtime transaction reentered during Swift SDK allocation capture');
    if (nativePathQueueOwner !== null) fail('runtime transaction reentered during a borrowed native path lifetime');
    const phasePermitRequested = payload !== null && typeof payload === 'object' &&
      Object.prototype.hasOwnProperty.call(payload, 'pre_actuation_ack');
    if (payload && payload.request && Array.isArray(payload.request.operations) && payload.request.operations.some(operation =>
      operation && typeof operation.op === 'string' && operation.op.startsWith('memory_'))) {
      if (phasePermitRequested) fail('private phase permit cannot select the raw memory reader');
      return inspectMemory(payload);
    }
    if (!ObjC.available || Process.platform !== 'darwin') fail('Objective-C runtime is unavailable');
    if (Process.id !== payload.pid || Process.mainModule.path !== payload.executable)
      fail('injected process does not match the exact target executable/PID');
    const request = payload.request;
    if (!request || !Array.isArray(request.operations) || request.operations.length < 1 || request.operations.length > 16)
      fail('runtime transaction operation budget is invalid');
    const receiverHypothesisRequested = containsReceiverDeclaration(request);
    function assertReceiverRequest() {
      const operation = request.operations[0], first = operation && operation.arguments && operation.arguments[0];
      const recipe = first && first.type === 'pointer' && first.value && first.value.native_path;
      const v1 = recipe && recipe.format === RECEIVER_HYPOTHESIS_FORMAT && recipe.lifetime && recipe.lifetime.mode === RECEIVER_HYPOTHESIS_MODE;
      const v2 = recipe && recipe.format === RECEIVER_HYPOTHESIS_V2_FORMAT && recipe.lifetime && recipe.lifetime.mode === RECEIVER_HYPOTHESIS_V2_MODE;
      if (!dataEqual(Object.keys(request), ['operations']) || request.operations.length !== 1 ||
          !operation || operation.op !== 'native_invoke' || (!v1 && !v2))
        fail('receiver hypothesis permits one dedicated direct native invocation only');
    }
    const result = {status: 'incomplete', operations: [], native_calls: 0, invocations: 0,
      helper_load_attempts: 0, callable_resolutions: 0, helper_residency: [],
      error: null, handles_expired: false, queue: 'main', native_state_uncertain: false,
      process: {pid: Process.id, executable: Process.mainModule.path, architecture: Process.arch}};
    let Swift = null, TargetStructMetadata = null;
    if (!receiverHypothesisRequested && requestNeedsSwift(request)) {
      try {
        if (Date.now() >= payload.deadline_ms) fail('runtime deadline expired before Swift initialization');
        const bridge = await import('frida-swift-bridge');
        const metadata = await import('frida-swift-bridge/dist/abi/metadata.js');
        if (!bridge.default || typeof metadata.TargetStructMetadata !== 'function')
          fail('Swift bridge did not expose its fixed runtime/metadata interface');
        Swift = bridge.default;
        TargetStructMetadata = metadata.TargetStructMetadata;
        if (Date.now() >= payload.deadline_ms) fail('runtime deadline expired during Swift initialization');
      } catch (error) {
        // Initialization is before every requested operation, including getters.
        // No transaction handles or task invocation exist on this failure path.
        result.error = {...errorInfo(error), stage: 'swift_initialization'};
        result.handles_expired = true;
        send({event: 'swift-initialization-failed', error: result.error});
        return result;
      }
    }
    const aliases = new Map();
    const retained = new Map();
    const borrowed = new Map();
    const nativeAliases = new Map();
    const allocations = [];
    let sdkReturnStorage = new WeakMap();
    const sdkAllocationOwners = [];
    const sdkCaptureToken = {};
    let sdkAllocationBytes = 0;
    const pointerReads = [];
    const swiftFieldReads = [];
    const callableEntries = [];
    const nativePaths = [];
    // Tokens never cross RPC or enter a receipt. Each permit belongs to one
    // actual call object/slot in this transaction and is consumed before entry.
    const nativePathLeases = new Map();
    const nativePathPermits = new Map();
    const nativePathCalls = new Map();
    let qtMocOperationTokens = new WeakMap();
    const borrowedRootScopes = new Map();
    const nativePathTransaction = Object.freeze({});
    let nativePathActive = false;
    let lifecycleObserver = null;
    let phasePermit = null;
    let targetCallEpoch = 0, activeOperation = null, activeOperationIndex = -1;
    let nextHandle = 1;
    function deadline() {
      if (Date.now() >= payload.deadline_ms) fail('runtime deadline expired; refusing queued native work');
    }
    function makePhasePermit(config, onFailure) {
      const protocol = 'anyuse.rea-phased-permit/1', frameFormat = 'anyuse.rea-phased-permit-frame/1';
      const require_ = (condition, message) => { if (!condition) fail(message); };
      const keys = (value, fields) => require_(value !== null && typeof value === 'object' && !Array.isArray(value) &&
        dataEqual(Object.keys(value).sort(), fields.slice().sort()), 'closed private phase permit schema required');
      const integer = (value, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) =>
        require_(Number.isSafeInteger(value) && value >= minimum && value <= maximum, 'exact bounded phase permit integer required');
      const text = (value, maximum, empty = false) => {
        require_(typeof value === 'string' && (empty || value.length > 0) && value.indexOf('\0') === -1 &&
          Array.from(value).length <= maximum, 'bounded literal phase permit text required');
        let bytes = 0;
        for (let index = 0; index < value.length; index++) {
          const code = value.charCodeAt(index);
          if (code >= 0xd800 && code <= 0xdbff) {
            require_(index + 1 < value.length && value.charCodeAt(index + 1) >= 0xdc00 && value.charCodeAt(index + 1) <= 0xdfff,
              'literal UTF-8 phase permit text required'); index++; bytes += 4;
          } else {
            require_(code < 0xdc00 || code > 0xdfff, 'literal UTF-8 phase permit text required');
            bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : 3;
          }
        }
        return bytes;
      };
      const sha = value => require_(typeof value === 'string' && /^[0-9a-f]{64}$/.test(value), 'exact phase permit digest/nonce required');
      const path = value => {
        text(value, 4096);
        require_(value[0] === '/' && value !== '/' && value.split('/').slice(1).every(part => part.length > 0 && part !== '.' && part !== '..'),
          'canonical absolute phase permit path required');
      };
      const bindingKeys = ['format', 'run_nonce', 'request_sha256', 'identity_sha256', 'target_sha256', 'adapter_sha256',
        'adapter_source_sha256', 'adapter_manifest_sha256', 'native_resources_sha256', 'compiler_provenance_sha256',
        'observer_sha256', 'observer_capability', 'pid', 'executable', 'deadline_ms'];
      const validateBinding = binding => {
        keys(binding, bindingKeys);
        require_(binding.format === 'anyuse.rea-phased-permit-binding/1', 'current private permit binding required');
        for (const name of bindingKeys.filter(name => !['format', 'observer_capability', 'pid', 'executable', 'deadline_ms'].includes(name))) sha(binding[name]);
        require_(['ax_focused_readonly/1', 'ax_owned_visible_readonly/1'].includes(binding.observer_capability), 'fixed phase observer capability required');
        integer(binding.pid, 1, 0x7fffffff); integer(binding.deadline_ms, 1); path(binding.executable);
      };
      const selection = value => { keys(value, ['generation', 'owner_thread_id']); integer(value.generation, 1, 4096); integer(value.owner_thread_id, 1); };
      const pin = value => { keys(value, ['file', 'sha256', 'bytes']); path(value.file); sha(value.sha256); integer(value.bytes, 1, 129 * 1024 * 1024); };
      const digest = value => Checksum.compute('sha256', canonicalJSON(value));
      keys(config, ['format', 'binding', 'binding_sha256', 'worker_pid', 'worker_nonce', 'observer_selector', 'observer_spec',
        'request_canonical_json', 'observer_canonical_json', 'permit_wait_ms']);
      config = pathFreeze(pathCopy(config));
      require_(config.format === protocol, 'current private phase permit protocol required');
      validateBinding(config.binding); sha(config.binding_sha256); sha(config.worker_nonce); integer(config.worker_pid, 1, 0x7fffffff);
      require_(config.worker_pid !== Process.id && config.permit_wait_ms === 5000 && Number.isSafeInteger(config.permit_wait_ms),
        'one fixed bounded worker phase permit required');
      const binding = pathFreeze(pathCopy(config.binding)), workerNonce = config.worker_nonce;
      const record = result.pre_actuation_ack = {format: 'anyuse.rea-phased-permit-agent/1', binding_sha256: digest(binding),
        worker_nonce: workerNonce, selection: null, decision_sha256: null, state: 'prepared', permit_consumed: false,
        native_entry: false, closed: false, events: [], error: null};
      let timer = null, waiting = null, armed = false, decisionSeen = false, decision = null;
      let requestSnapshot = null;
      const event = kind => {
        const at = Date.now(), previous = record.events.length ? record.events[record.events.length - 1].at : null;
        require_(record.events.length < 32 && Number.isFinite(at) && at >= 0 && at <= 253402300800000 && (previous === null || at >= previous),
          'bounded ordered local phase permit events required');
        record.events.push({seq: record.events.length + 1, kind, at});
      };
      event('prepared');
      const cancelTimer = () => { if (timer !== null) { const active = timer; timer = null; clearTimeout(active); } };
      const unavailable = (state, kind, error) => {
        if (record.closed) return;
        record.state = state;
        if (record.error === null) record.error = {type: String(error.name || 'PhasePermitError').slice(0, 128), message: String(error.message || error).slice(0, 2048)};
        // A clock/journal or timer failure cannot strand the pending Promise.
        // Retain the actual prefix; missing closure events never prove success.
        try { event(kind); } catch (failure) { error = failure; record.state = 'failed'; }
        try { cancelTimer(); } catch (failure) { error = failure; record.state = 'failed'; }
        const resolve = waiting; waiting = null;
        if (resolve !== null) resolve(false);
        onFailure(error);
      };
      const guard = work => {
        if (record.closed) return;
        try { work(); } catch (error) { unavailable('failed', 'error', error); }
      };
      try {
        require_(record.binding_sha256 === config.binding_sha256, 'worker binding digest differs from actual private data');
        require_(binding.pid === Process.id && binding.pid === payload.pid && binding.executable === Process.mainModule.path &&
          binding.executable === payload.executable && binding.deadline_ms === payload.deadline_ms, 'private permit process/deadline binding changed');
        deadline();
        require_(request.operations.length === 1 && ['native_invoke', 'qt_moc_invoke'].includes(request.operations[0].op),
          'private permit requires one actual observed native/Qt invocation');
        text(config.request_canonical_json, 8 * 1024 * 1024); text(config.observer_canonical_json, 8 * 1024 * 1024);
        require_(Checksum.compute('sha256', config.request_canonical_json) === binding.request_sha256 &&
          dataEqual(JSON.parse(config.request_canonical_json), request), 'private permit changed actual request or canonical request hash');
        requestSnapshot = pathFreeze(JSON.parse(config.request_canonical_json));
        require_(digest(payload.target) === binding.target_sha256 && digest(payload.native_resources) === binding.native_resources_sha256,
          'private permit target/resource data changed');
        require_(payload.compiler_provenance !== null && typeof payload.compiler_provenance === 'object' &&
          !Array.isArray(payload.compiler_provenance) && digest(payload.compiler_provenance) === binding.compiler_provenance_sha256,
          'private permit compiler provenance is absent or changed');
        const selector = config.observer_selector;
        keys(selector, ['format', 'role', 'subrole', 'title', 'geometry']);
        require_(selector.format === 'anyuse.rea-ax-window-selector/1' && selector.role === 'AXWindow', 'fixed phase observer selector required');
        for (const name of ['subrole', 'title']) if (selector[name] !== null) text(selector[name], 1024, name === 'title');
        const geometry = selector.geometry;
        if (geometry && geometry.kind === 'full_display') keys(geometry, ['kind']);
        else {
          keys(geometry, ['kind', 'position', 'size']);
          require_(geometry.kind === 'exact', 'fixed phase observer geometry required');
          for (const name of ['position', 'size']) require_(Array.isArray(geometry[name]) && geometry[name].length === 2 &&
            geometry[name].every(value => typeof value === 'number' && Number.isFinite(value) && (name !== 'size' || value > 0)),
            'finite exact phase observer geometry required');
        }
        require_(Checksum.compute('sha256', config.observer_canonical_json) === binding.observer_sha256 &&
          dataEqual(JSON.parse(config.observer_canonical_json), {capability: binding.observer_capability, target: payload.target,
            selector, spec: config.observer_spec}), 'private permit observer descriptor differs from actual data');
      } catch (error) {
        record.state = 'failed'; record.error = {type: String(error.name || 'PhasePermitError').slice(0, 128), message: String(error.message || error).slice(0, 2048)};
        try { event('error'); } finally { record.closed = true; }
        event('closed'); throw error;
      }
      const validateFrame = frame => {
        keys(frame, ['format', 'kind', 'binding', 'worker_pid', 'worker_nonce', 'selection', 'decision', 'ordinal', 'setup', 'before', 'checkpoint', 'reason']);
        validateBinding(frame.binding);
        require_(frame.format === frameFormat && frame.kind === 'decision' && dataEqual(frame.binding, binding) &&
          frame.worker_pid === config.worker_pid && Number.isSafeInteger(frame.worker_pid) && frame.worker_nonce === workerNonce &&
          frame.ordinal === 1 && Number.isSafeInteger(frame.ordinal) && ['allow', 'deny'].includes(frame.decision),
          'decision does not belong to this private one-use worker binding');
        if (frame.selection !== null) selection(frame.selection);
        for (const name of ['setup', 'before', 'checkpoint']) if (frame[name] !== null) pin(frame[name]);
        if (frame.decision === 'allow') require_(frame.selection !== null && frame.setup !== null && frame.before !== null &&
          frame.checkpoint !== null && frame.reason === null, 'positive permit needs actual setup/before/checkpoint pins');
        else text(frame.reason, 256);
        require_(text(canonicalJSON(frame), 8192) <= 8192, 'private decision exceeds fixed UTF-8 frame bound');
      };
      return {
        bind(prepared) {
          require_(dataEqual(prepared.recipe.root.lifecycle, config.observer_spec), 'private permit must bind the actual prepared lifecycle specification');
        },
        arm() {
          require_(!armed && !record.closed, 'private permit receiver cannot be rearmed'); armed = true;
          recv('anyuse.rea-phase-permit', message => guard(() => {
            require_(!decisionSeen && record.state !== 'consumed', 'private phase decision cannot be reused'); decisionSeen = true;
            keys(message, ['type', 'payload']); require_(message.type === 'anyuse.rea-phase-permit', 'fixed private decision message required');
            validateFrame(message.payload); deadline();
            decision = pathFreeze(pathCopy(message.payload)); record.decision_sha256 = digest(decision);
            if (decision.decision === 'deny') { unavailable('denied', 'denied', new Error(decision.reason)); return; }
            require_(record.state === 'waiting' && waiting !== null && dataEqual(decision.selection, record.selection),
              'positive phase permit is early, stale or for another generation/owner');
            record.state = 'allowed'; event('permit_accepted'); cancelTimer();
            const resolve = waiting; waiting = null; resolve(true);
          }));
          recv('anyuse.rea-phase-permit-abort', message => guard(() => {
            keys(message, ['type', 'payload']); keys(message.payload, ['binding_sha256', 'worker_nonce', 'reason']);
            require_(message.type === 'anyuse.rea-phase-permit-abort' && message.payload.binding_sha256 === record.binding_sha256 &&
              message.payload.worker_nonce === workerNonce, 'abort belongs to another private transaction'); text(message.payload.reason, 256);
            if (record.permit_consumed) {
              event('late_abort'); record.error = {type: 'PhasePermitLateAbort', message: 'parent loss after commit cannot cancel native entry'};
              result.native_state_uncertain = true;
              if (result.error === null) result.error = {...record.error, stack: null};
              return;
            }
            unavailable('denied', 'denied', new Error(message.payload.reason));
          }));
        },
        wait(observer) {
          require_(armed && record.state === 'prepared' && !record.closed && nativePathLeases.size === 0 &&
            nativePathPermits.size === 0 && borrowedRootScopes.size === 0 && targetCallEpoch === 0 && !observer.acquired && !observer.consumed,
            'private wait cannot hold an acquired receiver/lease or an earlier target epoch');
          record.selection = {generation: observer.selected.ordinal, owner_thread_id: observer.owner}; selection(record.selection);
          record.state = 'waiting'; event('waiting');
          return new Promise(resolve => {
            waiting = resolve;
            const remaining = Math.min(config.permit_wait_ms, payload.deadline_ms - Date.now() - 3000);
            if (remaining <= 0) { unavailable('expired', 'expired', new Error('private phase permit cleanup reserve expired')); return; }
            timer = setTimeout(() => guard(() => unavailable('expired', 'expired', new Error('private phase permit wait expired'))), remaining);
            send({event: 'phase-permit-needed', binding_sha256: record.binding_sha256, worker_nonce: workerNonce,
              selection: pathCopy(record.selection)});
          });
        },
        consume(pathCall, pathInvocation, rootCapture) {
          deadline();
          const observer = lifecycleObserver;
          require_(!record.closed && record.state === 'allowed' && !record.permit_consumed && decision !== null &&
            pathCall !== null && pathInvocation === true && rootCapture === null && activeOperationIndex === 0 &&
            ['native_invoke', 'qt_moc_invoke'].includes(activeOperation.op) && observer !== null && !observer.poisoned &&
            observer.phase === 'consumed' && observer.consumed && observer.selected !== null && observer.selected.state === 'live' &&
            observer.callbacks === 0 && observer.frames.length === 0 && observer.owner === Process.getCurrentThreadId() &&
            dataEqual(request, requestSnapshot) &&
            dataEqual(record.selection, {generation: observer.selected.ordinal, owner_thread_id: observer.owner}) &&
            dataEqual(decision.selection, record.selection), 'private positive phase permit cannot enter this exact native use');
          record.permit_consumed = true; record.state = 'consumed'; event('permit_consumed');
          record.native_entry = true; event('native_entry');
        },
        returned() { event('native_returned'); if (record.error !== null) fail(record.error.message); },
        close() {
          cancelTimer(); waiting = null;
          if (record.closed) return;
          if (record.error === null && result.error !== null) {
            record.error = {type: String(result.error.type || 'PhasePermitError').slice(0, 128), message: String(result.error.message || '').slice(0, 2048)};
            event('error');
          }
          if (!['consumed', 'denied', 'expired', 'failed'].includes(record.state)) {
            record.state = 'failed';
            if (record.error === null) { record.error = {type: 'PhasePermitError', message: 'transaction closed before native permit consumption'}; event('error'); }
          }
          record.closed = true; decision = null; event('closed');
        }
      };
    }
    function targetCall(function_, arguments_, pathCall = null, rootCapture = null) {
      deadline();
      if (sdkAllocationCaptureOwner !== null && sdkAllocationCaptureOwner !== sdkCaptureToken)
        fail('native work cannot enter another transaction\'s Swift SDK allocation capture');
      if (result.native_calls >= 4096 || targetCallEpoch >= 4096) fail('requested native-call budget exhausted');
      const scope = rootCapture === null ? null : borrowedRootScopes.get(rootCapture);
      if (rootCapture !== null && (!scope || scope.phase !== 'acquiring' || scope.function !== function_ ||
          scope.transaction !== nativePathTransaction || pathCall !== null || arguments_.length !== (scope.expectedArgsLength ?? 0)))
        fail('borrowed root raw-return capture is not bound to its exact getter');
      const pathInvocation = pathCall !== null && consumeNativePathPermits(pathCall, function_, arguments_);
      if (phasePermit !== null) phasePermit.consume(pathCall, pathInvocation, rootCapture);
      const callEpoch = ++targetCallEpoch;
      result.native_calls++;
      if (pathInvocation) result.invocations++;
      try {
        const value = function_(...arguments_);
        if (phasePermit !== null) phasePermit.returned();
        if (scope !== null) {
          // Record THIS call's epoch at the raw return, before postprocessing or
          // events. A reentrant/intervening targetCall cannot become its epoch.
          scope.getterEpoch = callEpoch; scope.pointer = value; scope.phase = 'ready';
        }
        return value;
      }
      catch (error) {
        // Stealing a native fault does not restore the process. No automatic
        // retry or later agent actuation may follow this receipt.
        result.native_state_uncertain = true;
        throw error;
      }
    }
    function hold(object, alias) {
      if (!object || object.handle.isNull()) fail('native object is nil');
      if (object.$kind !== 'instance') fail('only instance objects get ephemeral handles');
      const address = object.handle.toString();
      let entry = retained.get(address);
      if (entry === undefined) {
        if (retained.size >= 128) fail('object handle budget exhausted');
        if (!object.$methods.includes('- retain') || !object.$methods.includes('- release'))
          fail('object has no standard lifetime interface');
        object['- retain']();
        entry = {object, id: 'h' + nextHandle++};
        retained.set(address, entry);
      }
      if (alias !== undefined) {
        if (aliases.has(alias)) fail('object alias already exists');
        aliases.set(alias, object);
      }
      return {handle: entry.id, class: object.$className, module: classModule(object),
        alias: alias === undefined ? null : alias, expires: 'transaction'};
    }
    function unique(klass, subclasses) {
      const matches = [];
      ObjC.choose({class: klass, subclasses}, {
        onMatch(instance) {
          deadline();
          hold(instance);
          matches.push(instance);
          if (matches.length >= 2) return 'stop';
        }, onComplete() {}
      });
      if (matches.length !== 1) fail('need one unique live instance; found ' + (matches.length >= 2 ? 'at least 2' : '0'));
      return matches[0];
    }
    function receiverOf(specification) {
      let receiver;
      if (specification.alias !== undefined) {
        receiver = aliases.get(specification.alias);
        if (receiver === undefined) fail('unknown or expired transaction alias');
      } else {
        const klass = classNamed(specification.class);
        receiver = specification.kind === 'class' ? klass : unique(klass, specification.subclasses === true);
      }
      for (const pathStep of specification.path || []) {
        deadline();
        const found = methodOf(receiver, pathStep.selector, pathStep.signature, [], 'object');
        const next = targetCall(found.method.bind(receiver), []);
        if (!next || !next.handle || next.handle.isNull()) fail('receiver path returned nil');
        hold(next);
        receiver = next;
      }
      return receiver;
    }
    function argumentValue(argument) {
      if (argument.type === 'nil') return ptr(0);
      if (argument.type === 'object') {
        const object = aliases.get(argument.value.alias);
        if (object === undefined) fail('argument object alias is unavailable');
        return object;
      }
      if (argument.type === 'utf8') {
        return ObjC.classes.NSString.stringWithUTF8String_(Memory.allocUtf8String(argument.value));
      }
      if (argument.type === 'int64' || argument.type === 'long') return int64(argument.value);
      if (argument.type === 'uint64' || argument.type === 'ulong') return uint64(argument.value);
      return argument.value;
    }
    function serialize(value, type, alias) {
      if (type === 'void') return null;
      if (type === 'object') return (!value || !value.handle || value.handle.isNull()) ? null : hold(value, alias);
      if (type === 'utf8') {
        if (!value || !value.handle || value.handle.isNull()) return null;
        // Never call description/toString on an arbitrary app object.
        let klass = value.$class, isString = false;
        for (let count = 0; klass !== null && count < 64; count++, klass = klass.$superClass) {
          if (klass.$className === 'NSString') { isString = true; break; }
        }
        if (!isString) fail('declared UTF-8 result is not NSString');
        const lengthMethod = methodOf(value, 'length', undefined, [], Process.pointerSize === 8 ? 'uint64' : 'uint32');
        const units = Number(lengthMethod.method.call(value));
        if (!Number.isSafeInteger(units) || units < 0 || units > MAX_STRING_UNITS) fail('NSString result exceeds its byte budget');
        const sizeType = Process.pointerSize === 8 ? 'uint64' : 'uint32';
        const sizeArgument = {type: sizeType, value: Process.pointerSize === 8 ? '4' : 4};
        const sizeMethod = methodOf(value, 'lengthOfBytesUsingEncoding:', undefined, [sizeArgument], sizeType);
        const bytes = Number(sizeMethod.method.call(value, argumentValue(sizeArgument)));
        if (!Number.isSafeInteger(bytes) || bytes < 0 || bytes > 4 * MAX_STRING_UNITS) fail('NSString UTF-8 bytes exceed their budget');
        if (!value.$methods.includes('- UTF8String')) fail('NSString has no listed UTF8String method');
        // Frida's ObjC '*' conversion returns a JavaScript C string, losing
        // embedded NUL bytes. Obtain the pointer through this one fixed,
        // checked Foundation signature; no pointer-return request is exposed.
        const utf8Method = value['- UTF8String'];
        const utf8Parts = tokens(utf8Method.types);
        if (utf8Parts.length !== 3 || utf8Parts[0] !== '*' || utf8Parts[1] !== '@' || utf8Parts[2] !== ':')
          fail('NSString UTF8String has an unexpected runtime ABI');
        const utf8Pointer = new NativeFunction(ObjC.api.objc_msgSend, 'pointer', ['pointer', 'pointer'],
          {exceptions: 'propagate'});
        const pointer = utf8Pointer(value.handle, ObjC.selector('UTF8String'));
        if (pointer.isNull()) fail('NSString conversion returned null');
        const text = bytes === 0 ? '' : pointer.readUtf8String(bytes);
        if (text.length !== units) fail('NSString conversion was truncated or lossy');
        return text;
      }
      if (type === 'bool') return Boolean(value);
      if (['int64', 'uint64', 'long', 'ulong'].includes(type)) return value.toString();
      const number = Number(value);
      if (!Number.isFinite(number)) fail('native scalar result is nonfinite');
      return number;
    }
    function holdPointer(pointer, alias) {
      if (pointer.isNull()) {
        if (alias !== undefined) fail('null borrowed pointer cannot introduce an alias');
        return null;
      }
      const key = pointer.toString();
      let id = borrowed.get(key);
      if (id === undefined) {
        if (borrowed.size >= 128) fail('borrowed native handle budget exhausted');
        id = 'n' + nextHandle++;
        borrowed.set(key, id);
      }
      if (alias !== undefined) {
        if (nativeAliases.has(alias) || aliases.has(alias)) fail('native alias already exists');
        nativeAliases.set(alias, pointer);
      }
      return {handle: id, alias: alias === undefined ? null : alias,
        ownership: 'borrowed_no_escape', expires: 'transaction'};
    }
    function readable(pointer, bytes) {
      if (pointer.isNull() || !Number.isSafeInteger(bytes) || bytes < 1 || bytes > 16384)
        fail('native memory read is null or exceeds its byte budget');
      const range = Process.findRangeByAddress(pointer);
      if (range === null || !range.protection.includes('r') ||
          pointer.add(bytes).compare(range.base.add(range.size)) > 0)
        fail('declared native memory region is not wholly readable');
      return pointer;
    }
    const imageCache = new Map();
    function word(pointer) {
      const value = Number(pointer.readU64().toString());
      if (!Number.isSafeInteger(value) || value < 0) fail('runtime metadata word exceeds its bounded numeric representation');
      return value;
    }
    function boundedText(pointer, maximum = 256, image = null) {
      for (let length = 0; length <= maximum; length++) {
        const byte = image === null ? readable(pointer.add(length), 1) : inImage(image, pointer.add(length), 1);
        if (byte.readU8() === 0) {
          if (image !== null) inImage(image, pointer, length + 1);
          return length === 0 ? '' : pointer.readUtf8String(length);
        }
      }
      fail('runtime metadata string exceeds its byte bound');
    }
    function imageFor(module) {
      const cached = imageCache.get(module.path);
      if (cached !== undefined) return cached;
      readable(module.base, 32);
      if (module.base.readU32() !== 0xfeedfacf) fail('loaded native module requires supported 64-bit Mach-O header');
      const cpu = module.base.add(4).readU32(), subtype = module.base.add(8).readU32();
      const commands = module.base.add(16).readU32(), bytes = module.base.add(20).readU32();
      if (commands < 1 || commands > 4096 || bytes < 8 || bytes > 1024 * 1024 || 32 + bytes > module.size)
        fail('loaded native module header exceeds its command bounds');
      let offset = 32, uuid = null, imageVmaddr = null;
      const sections = [], segments = [];
      for (let index = 0; index < commands; index++) {
        deadline();
        if (offset + 8 > 32 + bytes) fail('loaded native module command is incomplete');
        const pointer = readable(module.base.add(offset), 8);
        const kind = pointer.readU32(), width = pointer.add(4).readU32();
        if (width < 8 || offset + width > 32 + bytes) fail('loaded native module command exceeds its header');
        if (kind === 0x1b) {
          if (width !== 24 || uuid !== null) fail('loaded native image UUID is malformed/ambiguous');
          readable(pointer, 24);
          uuid = Array.from(new Uint8Array(pointer.add(8).readByteArray(16))).map(byte => byte.toString(16).padStart(2, '0')).join('');
        } else if (kind === 0x19) {
          readable(pointer, 72);
          const segmentVmaddr = word(pointer.add(24)), segmentBytes = word(pointer.add(32));
          const fileOffset = word(pointer.add(40)), fileBytes = word(pointer.add(48));
          const maximum = pointer.add(56).readU32(), initial = pointer.add(60).readU32();
          const count = pointer.add(64).readU32();
          if (count > 4096 || width !== 72 + count * 80) fail('loaded Mach-O section table exceeds its bound');
          if (fileOffset === 0 && fileBytes >= 32 + bytes) {
            if (imageVmaddr !== null) fail('loaded Mach-O image-header segment is ambiguous');
            imageVmaddr = segmentVmaddr;
          }
          const rawSegment = Array.from(new Uint8Array(pointer.add(8).readByteArray(16)));
          const segmentName = String.fromCharCode(...rawSegment.slice(0, rawSegment.indexOf(0) < 0 ? 16 : rawSegment.indexOf(0)));
          if (segmentBytes > 0 && !(segmentVmaddr === 0 && fileBytes === 0 && maximum === 0 && initial === 0))
            segments.push({segment: segmentName, address: segmentVmaddr, bytes: segmentBytes, file_offset: fileOffset,
              file_bytes: fileBytes, max_protection: maximum, initial_protection: initial});
          for (let sectionIndex = 0; sectionIndex < count; sectionIndex++) {
            const section = readable(pointer.add(72 + sectionIndex * 80), 80);
            const raw = Array.from(new Uint8Array(section.readByteArray(32)));
            const nameOf = start => String.fromCharCode(...raw.slice(start, start + 16).filter((byte, index, all) => index < (all.indexOf(0) < 0 ? all.length : all.indexOf(0))));
            const address = word(section.add(32)), sectionBytes = word(section.add(40));
            if (address < segmentVmaddr || address + sectionBytes > segmentVmaddr + segmentBytes)
              fail('loaded Mach-O section exceeds its declared segment');
            sections.push({segment: nameOf(16), name: nameOf(0), address, bytes: sectionBytes,
              file_offset: section.add(48).readU32(), flags: section.add(64).readU32()});
          }
        }
        offset += width;
      }
      const identity = {cpu_type: cpu, cpu_subtype: subtype, uuid};
      if (uuid === null || offset !== 32 + bytes) fail('loaded Mach-O requires one exact image UUID');
      const sectionFileOffsets = new Map();
      const relativeSections = sections.filter(section => section.bytes > 0).map(section => {
        if (imageVmaddr === null || section.address < imageVmaddr || section.address - imageVmaddr + section.bytes > 512 * 1024 * 1024)
          fail('loaded Mach-O section lacks bounded image-relative provenance');
        const {address, file_offset, ...metadata} = section;
        const relative = {...metadata, offset: '0x' + (address - imageVmaddr).toString(16)};
        // File offsets stay private: existing section pins keep their schema.
        sectionFileOffsets.set(relative, file_offset);
        return relative;
      });
      const relativeSegments = segments.map(segment => {
        if (imageVmaddr === null || segment.address < imageVmaddr || segment.address - imageVmaddr + segment.bytes > 512 * 1024 * 1024)
          fail('loaded Mach-O positive segment exceeds its declared image budget');
        const {address, ...metadata} = segment;
        return {...metadata, offset: '0x' + (address - imageVmaddr).toString(16)};
      }).sort((a, b) => Number(a.offset) - Number(b.offset));
      relativeSegments.forEach((segment, index) => {
        if (!segment.segment || index > 0 && Number(segment.offset) < Number(relativeSegments[index - 1].offset) + relativeSegments[index - 1].bytes)
          fail('loaded Mach-O positive segments are unnamed or overlapping');
      });
      const extent = relativeSegments.reduce((end, segment) => Math.max(end, Number(segment.offset) + segment.bytes), 0);
      const image = {module, identity, sections: relativeSections, sectionFileOffsets, segments: relativeSegments, extent};
      imageCache.set(module.path, image);
      return image;
    }
    function sameImage(a, b) { return a.cpu_type === b.cpu_type && a.cpu_subtype === b.cpu_subtype && a.uuid === b.uuid; }
    function moduleFor(claim) {
      const pin = (payload.native_resources || {})[claim.path];
      if (!pin || pin.file !== claim.path || pin.sha256 !== claim.sha256)
        fail('native module is not pinned by the independently validated worker');
      const modules = Process.enumerateModules().filter(module => module.path === claim.path);
      if (modules.length !== 1) fail('need one exact loaded native module');
      const image = imageFor(modules[0]);
      if (!Array.isArray(pin.macho_images) || !pin.macho_images.some(identity => sameImage(identity, image.identity)))
        fail('loaded Mach-O UUID differs from the pinned file image; stale-image execution refused');
      const declared = Array.isArray(pin.macho_segments) && pin.macho_segments.filter(item => sameImage(item.image_identity, image.identity));
      if (!declared || declared.length !== 1 || declared[0].segments.length !== image.segments.length ||
          !image.segments.every((segment, index) => ['segment', 'offset', 'bytes', 'file_offset', 'file_bytes', 'max_protection', 'initial_protection']
            .every(key => declared[0].segments[index][key] === segment[key])))
        fail('loaded Mach-O mapped segment declarations differ from the frozen file');
      return image;
    }
    function instructionFor(image, address, pin) {
      const relative = Number(address.sub(image.module.base).toString());
      const sections = image.sections.filter(section => section.bytes > 0 && (section.flags & 0x80000400) !== 0 &&
        relative >= Number(section.offset) && relative < Number(section.offset) + section.bytes);
      if (sections.length !== 1) fail('native entry is not inside one actual Mach-O instruction section');
      const section = sections[0];
      const pinned = pin && Array.isArray(pin.macho_sections) && pin.macho_sections.filter(item => sameImage(item.image_identity, image.identity));
      if (pin && (!pinned || pinned.length !== 1 || !pinned[0].sections.some(item =>
          item.segment === section.segment && item.name === section.name && item.offset === section.offset &&
          item.bytes === section.bytes && item.flags === section.flags)))
        fail('loaded native instruction section differs from the frozen file section');
      return section;
    }
    const canonicalSwiftTypes = {
      'Swift.Optional<Swift.OpaquePointer>': 's13OpaquePointerVSg', 'Swift.OpaquePointer': 's13OpaquePointerV',
      'Swift.Bool': 'Sb', 'Swift.Int8': 's4Int8V', 'Swift.UInt8': 's5UInt8V',
      'Swift.Int16': 's5Int16V', 'Swift.UInt16': 's6UInt16V', 'Swift.Int32': 's5Int32V',
      'Swift.UInt32': 's6UInt32V', 'Swift.Int64': 's5Int64V', 'Swift.UInt64': 's6UInt64V',
      'Swift.Int': 'Si', 'Swift.UInt': 'Su'
    };
    const integerSwiftTypes = {int8: 'Swift.Int8', uint8: 'Swift.UInt8', int16: 'Swift.Int16', uint16: 'Swift.UInt16',
      int32: 'Swift.Int32', uint32: 'Swift.UInt32', int64: 'Swift.Int64', uint64: 'Swift.UInt64',
      long: 'Swift.Int', ulong: 'Swift.UInt', pointer: 'Swift.OpaquePointer'};
    const canonicalMetadata = new Map(), structMetadata = new Map();
    let typeResolver = null;
    function swiftAvailable() {
      if (Process.arch !== 'arm64' || Process.pointerSize !== 8 || !Swift.available)
        fail('typed Swift metadata requires available ARM64 Darwin runtime');
    }
    function inImage(image, pointer, bytes) {
      const offset = Number(pointer.sub(image.module.base).toString());
      if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(bytes) || bytes < 1 ||
          !image.segments.some(segment => offset >= Number(segment.offset) && offset + bytes <= Number(segment.offset) + segment.bytes))
        fail('Swift metadata declaration exceeds its exact loaded image');
      return readable(pointer, bytes);
    }
    function relative(pointer, indirect = false) {
      readable(pointer, 4);
      const offset = pointer.readS32();
      if (offset === 0) return ptr(0);
      const address = pointer.add(indirect ? offset & ~1 : offset);
      return indirect && (offset & 1) !== 0 ? readable(address, 8).readPointer() : address;
    }
    function mangledLength(pointer, image) {
      // Symbolic references can contain NUL bytes. Keep their original image
      // addresses; copying to a C string destroys relative-reference context.
      let length = 0;
      while (length <= 1024) {
        const byte = inImage(image, pointer.add(length), 1).readU8();
        if (byte === 0) return length;
        const width = byte >= 1 && byte <= 0x17 ? 5 : byte >= 0x18 && byte <= 0x1f ? 1 + Process.pointerSize : 1;
        inImage(image, pointer.add(length), width);
        length += width;
      }
      fail('Swift mangled type bytes exceed the reflection bound');
    }
    function resolveSwiftType(pointer, length, context) {
      swiftAvailable();
      if (typeResolver === null) {
        const runtimes = Process.enumerateModules().filter(module => module.name === 'libswiftCore.dylib');
        if (runtimes.length !== 1) fail('need one loaded canonical Swift runtime');
        const runtime = runtimes[0], address = runtime.findExportByName('swift_getTypeByMangledNameInContext');
        const owner = address === null ? null : Process.findModuleByAddress(address);
        const range = address === null ? null : Process.findRangeByAddress(address);
        if (owner === null || owner.path !== runtime.path || range === null || !range.protection.includes('x'))
          fail('fixed Swift context type resolver is unavailable');
        // The official runtime declaration is swiftcall, not a guessed C ABI.
        typeResolver = Swift.NativeFunction(address, 'pointer', ['pointer', 'uint64', 'pointer', 'pointer']);
      }
      const metadata = targetCall(typeResolver, [pointer, uint64(String(length)), context, ptr(0)]);
      if (!metadata || metadata.isNull()) fail('Swift runtime could not resolve complete contextual type metadata');
      readable(metadata, 16);
      return metadata;
    }
    function canonicalSwift(typeName) {
      if (canonicalMetadata.has(typeName)) return canonicalMetadata.get(typeName);
      const mangled = canonicalSwiftTypes[typeName];
      if (mangled === undefined) fail('canonical Swift type is outside the supported reflected subset');
      const pointer = Memory.allocUtf8String(mangled);
      allocations.push(pointer);
      const metadata = resolveSwiftType(pointer, mangled.length, ptr(0));
      canonicalMetadata.set(typeName, metadata);
      return metadata;
    }
    function witnessLayout(metadata, expectedName = null) {
      const kind = metadata.readU32();
      if (![0x200, 0x201, 0x202].includes(kind)) fail('Swift value metadata kind has no supported witness layout');
      const table = readable(metadata.sub(8), 8).readPointer();
      readable(table, 0x58);
      const bytes = word(table.add(0x40)), stride = word(table.add(0x48)), flags = table.add(0x50).readU32();
      const forbidden = 0x03d10000;
      if ((flags & forbidden) !== 0 || bytes < 1 || bytes > 128 || stride < bytes || stride > 128)
        fail('Swift value is non-POD, noncopyable, address-sensitive, incomplete or outside the layout budget');
      const alignmentMask = flags & 255;
      if ((alignmentMask & (alignmentMask + 1)) !== 0 || alignmentMask > 15 || stride % (alignmentMask + 1) !== 0)
        fail('Swift value witness alignment/stride is unsupported');
      const verified = expectedName !== null && metadata.compare(canonicalSwift(expectedName)) === 0;
      if (expectedName !== null && !verified) fail('actual Swift field metadata differs from its exact canonical type');
      return {metadata_kind: kind, bytes, stride, alignment_mask: alignmentMask, flags,
        pod: true, bitwise_takable: true, copyable: true, bitwise_borrowable: true,
        addressable_for_dependencies: false, complete: true, canonical_type_verified: verified};
    }
    function descriptorInfo(image, descriptor) {
      inImage(image, descriptor, 20);
      const flags = descriptor.readU32(), kind = flags & 31;
      if (![16, 17, 18].includes(kind)) fail('Swift declaration is not a supported nominal class/struct/enum descriptor');
      const name = boundedText(relative(descriptor.add(8)), 256, image);
      let parent = relative(descriptor.add(4), true), moduleName = null, visited = 0;
      let generic = (flags & 0x80) !== 0;
      while (!parent.isNull() && visited++ < 8) {
        inImage(image, parent, 12);
        const parentFlags = parent.readU32();
        generic = generic || (parentFlags & 0x80) !== 0;
        if ((parentFlags & 31) === 0) {
          moduleName = boundedText(relative(parent.add(8)), 256, image);
          break;
        }
        parent = relative(parent.add(4), true);
      }
      if (moduleName === null) fail('Swift nominal declaration lacks a bounded module context');
      const fields = relative(descriptor.add(16));
      return {descriptor, flags, kind, name, moduleName, fields, image, generic};
    }
    function fieldRecords(info) {
      if (info.fields.isNull()) fail('Swift nominal declaration has no field reflection descriptor');
      inImage(info.image, info.fields, 16);
      const recordSize = info.fields.add(10).readU16(), count = info.fields.add(12).readU32();
      if (recordSize !== 12 || count > 128) fail('Swift field reflection record size/count is unsupported');
      const records = [];
      for (let index = 0; index < count; index++) {
        const record = inImage(info.image, info.fields.add(16 + index * recordSize), recordSize);
        const namePointer = relative(record.add(8)), typePointer = relative(record.add(4));
        if (namePointer.isNull() || typePointer.isNull()) fail('Swift reflected field has no exact name/type bytes');
        const name = boundedText(namePointer, 256, info.image);
        const length = mangledLength(typePointer, info.image);
        if (length < 1) fail('Swift reflected field type is empty');
        records.push({name, typePointer, length, index, mutable: (record.readU32() & 2) !== 0});
      }
      return records;
    }
    function nominalDescriptors(image) {
      const sections = image.sections.filter(section => section.name === '__swift5_types');
      if (sections.length === 0) return [];
      if (sections.length !== 1 || sections[0].bytes % 4 !== 0 || sections[0].bytes / 4 > 8192)
        fail('Swift nominal declaration table exceeds its bound');
      const section = sections[0], rows = [];
      for (let index = 0; index < section.bytes / 4; index++) {
        deadline();
        const entry = inImage(image, image.module.base.add(section.offset).add(index * 4), 4);
        const value = entry.readS32(), kind = value & 3;
        if (value === 0 || kind > 1) continue; // ObjC records are not Swift nominal declarations.
        let descriptor = entry.add(value & ~3);
        if (kind === 1) descriptor = readable(descriptor, 8).readPointer();
        if (descriptor.isNull()) continue;
        try { rows.push(descriptorInfo(image, descriptor)); }
        catch (error) { rows.push({descriptor, image, error: errorInfo(error)}); }
      }
      return rows;
    }
    function swiftType(specification, targetImage) {
      swiftAvailable();
      const key = targetImage.module.path + ':' + specification.module + ':' + specification.name;
      if (structMetadata.has(key)) {
        const known = structMetadata.get(key);
        if (JSON.stringify(known.specification) !== JSON.stringify(specification)) fail('one transaction declared conflicting Swift layouts');
        return known.metadata;
      }
      const matches = nominalDescriptors(targetImage).filter(info => info.kind === 17 &&
        info.moduleName === specification.module && info.name === specification.name);
      if (matches.length !== 1) fail('need one exact nongeneric Swift struct declaration in the pinned callable module');
      const info = matches[0], records = fieldRecords(info);
      if (info.generic) fail('generic Swift value ABI/context is unsupported');
      const typePointer = relative(info.fields), length = mangledLength(typePointer, targetImage);
      const metadataPointer = resolveSwiftType(typePointer, length, info.descriptor);
      const layout = witnessLayout(metadataPointer);
      // Swift 6.3.3 ABI Metadata.h: the five 32-bit TypeContextDescriptor
      // fields occupy 20 bytes. StructDescriptor then stores NumFields and
      // FieldOffsetVectorOffset at +20/+24, independently of the bridge's
      // incorrect +24/+28 descriptor convenience getters.
      const numFields = inImage(targetImage, info.descriptor.add(20), 4).readU32();
      if (layout.metadata_kind !== 0x200 || layout.stride !== specification.bytes || layout.stride < 8 || layout.stride % 8 !== 0 ||
          readable(metadataPointer.add(8), 8).readPointer().compare(info.descriptor) !== 0 ||
          records.length !== specification.fields.length || numFields !== records.length)
        fail('Swift aggregate metadata/field count/stride differs from its declared register layout');
      const vector = inImage(targetImage, info.descriptor.add(24), 4).readU32();
      // The metadata address point has Kind and Description as two pointer
      // words. Field offsets are uint32 entries beyond that fixed header.
      if (vector < 2 || vector > 131072) fail('Swift struct field offset vector is unavailable/unbounded');
      let end = 0;
      records.forEach((record, index) => {
        const declared = specification.fields[index], canonicalName = integerSwiftTypes[declared.type];
        if (record.name !== declared.name || canonicalName === undefined) fail('Swift register aggregate field declaration differs');
        const fieldMetadata = resolveSwiftType(record.typePointer, record.length, info.descriptor);
        const fieldLayout = witnessLayout(fieldMetadata, canonicalName);
        if (fieldLayout.metadata_kind !== 0x200) fail('Swift register aggregates support canonical integer/pointer leaves only');
        const offset = readable(metadataPointer.add(vector * 8 + index * 4), 4).readU32();
        if (offset < end || offset % (fieldLayout.alignment_mask + 1) !== 0 || offset + fieldLayout.bytes > layout.bytes)
          fail('Swift aggregate reflected field offset/layout exceeds its value witness bounds');
        end = offset + fieldLayout.bytes;
      });
      const metadata = new TargetStructMetadata(metadataPointer);
      structMetadata.set(key, {specification, metadata});
      return metadata;
    }
    function classReflection(recipe) {
      swiftAvailable();
      const image = moduleFor(recipe.module), receiver = receiverOf(recipe.receiver);
      if (receiver.$kind !== 'instance' || receiver.$className !== recipe.receiver_class)
        fail('Swift field reflection did not reach its exact declared NSObject-backed instance class');
      hold(receiver);
      const metadata = readable(receiver.$class.handle, 72);
      if ((metadata.add(32).readU32() & 2) === 0) fail('actual receiver class is not stable Swift class metadata');
      const instanceAddressPoint = metadata.add(44).readU32(), instanceBytes = metadata.add(48).readU32();
      const classSize = metadata.add(56).readU32(), classAddressPoint = metadata.add(60).readU32();
      const actualBytes = Number(ObjC.api.class_getInstanceSize(receiver.$class.handle));
      if (instanceAddressPoint !== 0 || instanceBytes < 8 || instanceBytes > 1024 * 1024 || actualBytes !== instanceBytes ||
          classSize < 72 || classSize > 1024 * 1024 || classAddressPoint > classSize || classSize - classAddressPoint < 72)
        fail('Swift class metadata address-point/instance/class extents are unavailable or inconsistent');
      const descriptor = metadata.add(64).readPointer(), info = descriptorInfo(image, descriptor);
      if (info.kind !== 16 || info.generic || info.name !== recipe.swift_class.name || info.moduleName !== recipe.swift_class.module)
        fail('actual Swift class descriptor differs from the frozen nongeneric module/class declaration');
      inImage(image, descriptor, 44);
      const numFields = descriptor.add(36).readU32(), records = fieldRecords(info);
      if (numFields !== records.length || numFields > 128) fail('Swift class descriptor/field reflection counts disagree');
      let vector = descriptor.add(40).readU32();
      if ((info.flags & 0x20000000) !== 0) {
        // For resilient superclasses the descriptor offset is relative to the
        // immediate-members area. Use initialized runtime bounds, not a guess.
        const bounds = relative(descriptor.add(24));
        if (bounds.isNull()) fail('resilient Swift class has no runtime metadata bounds');
        const immediateBytes = Number(inImage(image, bounds, 16).readS64().toString());
        if (!Number.isSafeInteger(immediateBytes) || immediateBytes === 0 || Math.abs(immediateBytes) > 1024 * 1024 || immediateBytes % 8 !== 0)
          fail('resilient Swift metadata bounds are uninitialized or unsupported');
        vector += immediateBytes / 8;
      }
      if (!Number.isSafeInteger(vector) || vector < 1 || vector > 131072 || vector * 8 + numFields * 8 > classSize - classAddressPoint)
        fail('Swift class field-offset vector exceeds its actual positive metadata extent');
      const offsets = records.map(record => word(readable(metadata.add(vector * 8 + record.index * 8), 8)));
      return {receiver, metadata, info, image, records, offsets, numFields, vector, instanceBytes, classSize, classAddressPoint};
    }
    function reflectedField(reflection, record, expectedName = null) {
      const metadata = resolveSwiftType(record.typePointer, record.length, reflection.info.descriptor);
      let typeName = expectedName;
      if (typeName === null) {
        for (const name of Object.keys(canonicalSwiftTypes)) {
          if (metadata.compare(canonicalSwift(name)) === 0) { typeName = name; break; }
        }
      }
      const layout = witnessLayout(metadata, typeName);
      const offset = reflection.offsets[record.index];
      if (!Number.isSafeInteger(offset) || offset < 8 || offset % (layout.alignment_mask + 1) !== 0 || offset + layout.bytes > reflection.instanceBytes)
        fail('reflected Swift field slot exceeds the actual instance layout/alignment');
      readable(reflection.receiver.handle.add(offset), layout.bytes);
      return {record, metadata, typeName, layout, offset};
    }
    function swiftFieldRead(recipe, requirePointer = true, rootLease = null) {
      const evidence = (payload.native_resources || {})[recipe.evidence.file];
      if (!evidence || evidence.sha256 !== recipe.evidence.sha256) fail('Swift field contract was not pinned before attachment');
      const reflection = classReflection(recipe);
      const find = name => {
        const matches = reflection.records.filter(record => record.name === name);
        if (matches.length !== 1) fail('need one exact reflected Swift field name');
        return matches[0];
      };
      const field = reflectedField(reflection, find(recipe.field.name), recipe.field.type_name);
      if (field.layout.metadata_kind !== 0x202 || field.layout.bytes !== 8 || field.layout.stride !== 8 || field.layout.alignment_mask !== 7 ||
          (recipe.field.offset !== undefined && recipe.field.offset !== field.offset))
        fail('Swift Optional<OpaquePointer> field does not have the verified canonical pointer-word layout');
      const preconditions = [];
      for (const condition of recipe.preconditions || []) {
        const observed = reflectedField(reflection, find(condition.name), condition.type_name);
        if (observed.layout.metadata_kind !== 0x200 || observed.layout.bytes !== 1 || observed.layout.stride !== 1 || observed.layout.alignment_mask !== 0)
          fail('Swift lifecycle precondition does not have canonical Bool byte layout');
        if (observed.offset >= field.offset && observed.offset < field.offset + 8)
          fail('Swift lifecycle field overlaps its native pointer slot');
        const raw = reflection.receiver.handle.add(observed.offset).readU8();
        if (raw !== 0 && raw !== 1) fail('Swift Bool field has an invalid observed representation');
        const value = raw === 1;
        preconditions.push({name: condition.name, type_name: condition.type_name, offset: observed.offset, index: observed.record.index,
          layout: {...observed.layout, encoding: 'bool_byte'}, raw_byte: raw, value, expected: condition.equals});
        if (value !== condition.equals) fail('observed Swift lifecycle precondition refuses the native operation');
      }
      const pointer = reflection.receiver.handle.add(field.offset).readPointer();
      if (rootLease !== null) {
        registerNativePathOwner(rootLease, reflection.receiver);
        rootLease.reads.push({slot: reflection.receiver.handle.add(field.offset), type: 'pointer', value: pointer});
        for (const condition of preconditions)
          rootLease.reads.push({slot: reflection.receiver.handle.add(condition.offset), type: 'bool', value: condition.value});
      }
      const observation = {source: 'swift_metadata', recipe, receiver_class: reflection.receiver.$className,
        swift_class: {module: reflection.info.moduleName, name: reflection.info.name},
        field: {name: field.record.name, type_name: field.typeName, offset: field.offset, index: field.record.index,
          num_fields: reflection.numFields, vector_offset_words: reflection.vector,
          descriptor_offset: reflection.info.descriptor.sub(reflection.image.module.base).toString(),
          mangled_type_offset: field.record.typePointer.sub(reflection.image.module.base).toString(), mangled_type_bytes: field.record.length + 1},
        image_identity: reflection.image.identity, module_size: reflection.image.extent, reported_module_size: reflection.image.module.size,
        instance_bytes: reflection.instanceBytes, class_size: reflection.classSize, class_address_point: reflection.classAddressPoint,
        layout: {...field.layout, encoding: 'zero_is_none'}, preconditions, pointer: holdPointer(pointer),
        lifetime: {receiver_retained: true, queue: 'main', native_handle_retained: false}};
      swiftFieldReads.push(observation);
      send({event: 'native-swift-field-read', ...observation});
      // This is the exact Optional<OpaquePointer> null-niche, not general enum
      // support. Retaining its ObjC owner never promises independent mpv lifetime.
      if (pointer.isNull() && requirePointer) fail('reflected Swift optional pointer is none; target native call refused');
      return {pointer, observation};
    }
    function nativeType(specification, abi, image) {
      if (typeof specification === 'string') {
        if (abi === 'swift' && ['float', 'double', 'bool', 'utf8'].includes(specification))
          fail('this Swift bridge has no verified FP/BOOL/string lowering');
        return specification === 'utf8' ? 'pointer' : specification;
      }
      if (specification.kind === 'struct') {
        if (abi === 'swift') fail('native Swift aggregates require actual metadata, not C layout arrays');
        return specification.fields.map(field => nativeType(field.type, abi, image));
      }
      if (specification.kind === 'buffer') return 'pointer';
      if (specification.kind === 'swift_type' && abi === 'swift') return swiftType(specification, image);
      fail('unsupported native ABI layout');
    }
    function entryFor(target, signatureEvidence, abi) {
      const resolved = moduleFor(target.module), module = resolved.module;
      const evidence = (payload.native_resources || {})[signatureEvidence.file];
      if (!evidence || evidence.sha256 !== signatureEvidence.sha256)
        fail('native prototype evidence was not pinned before attachment');
      let address;
      if (target.symbol !== undefined) {
        address = module.findSymbolByName(target.symbol);
        if (address === null) address = module.findExportByName(target.symbol);
        if (address === null) fail('exact native symbol is unavailable; no guessed fallback');
      } else {
        const offset = target.offset;
        if (ptr(offset).compare(ptr(resolved.extent)) >= 0) fail('native image offset exceeds the pinned declared image extent');
        address = module.base.add(offset);
      }
      const range = Process.findRangeByAddress(address);
      if (range === null || !range.protection.includes('x'))
        fail('resolved native entry is not executable code in the exact pinned module');
      const instructionSection = instructionFor(resolved, address, (payload.native_resources || {})[module.path]);
      if (abi === 'unix64' && Process.arch !== 'x64') fail('unix64 ABI requires an x64 target');
      return {address, image: resolved, metadata: {target, module: module.path, image_identity: resolved.identity,
        instruction_section: instructionSection, module_size: resolved.extent, reported_module_size: module.size,
        executable: true, symbol: target.symbol === undefined ? null : target.symbol,
        implementation_offset: address.sub(module.base).toString()}};
    }
    function callablePointer(recipe) {
      const target = {module: recipe.module};
      if (recipe.symbol !== undefined) target.symbol = recipe.symbol;
      else target.offset = recipe.offset;
      const resolved = entryFor(target, recipe.signature_evidence, recipe.abi);
      if (result.callable_resolutions >= 128) fail('native callable resolution budget exhausted');
      result.callable_resolutions++;
      const observed = {recipe, entry: resolved.metadata, declaration: {abi: recipe.abi,
        argument_types: recipe.argument_types, return_type: recipe.return_type, context: recipe.context,
        pointer_policy: recipe.pointer_policy}, pointer: holdPointer(resolved.address),
        wrapper_constructed: false, original_dispatched: false};
      callableEntries.push(observed);
      send({event: 'native-callable-resolved', ...observed});
      return {pointer: resolved.address, observation: observed};
    }
    function nativeFunction(specification) {
      const entry = entryFor(specification.target, specification.signature_evidence, specification.abi);
      const resolved = entry.image, module = resolved.module, address = entry.address;
      if (specification.compiler_adapter !== undefined) {
        const claim = specification.compiler_adapter.evidence, evidence = (payload.native_resources || {})[claim.file];
        if (!evidence || evidence.sha256 !== claim.sha256) fail('compiler adapter completion/build evidence was not pinned');
      }
      const returnType = nativeType(specification.return_type, specification.abi, resolved);
      const argTypes = specification.arguments.map(argument => nativeType(argument.type, specification.abi, resolved));
      let function_;
      if (specification.abi === 'swift') {
        if (Process.arch !== 'arm64' || !Swift.available) fail('native Swift bridge is unavailable for this target architecture');
        // The bridge currently lowers integer registers; async/generic/throws,
        // FP and ownership-heavy types are intentionally not guessed.
        const context = specification.context === undefined ? undefined : pointerValue(specification.context.value);
        function_ = Swift.NativeFunction(address, returnType, argTypes, context);
      } else {
        function_ = new NativeFunction(address, returnType, argTypes,
          {abi: specification.abi, scheduling: 'cooperative', exceptions: 'steal'});
      }
      return {function_, image: resolved, address, metadata: {
        ...entry.metadata, abi: specification.abi,
        argument_types: specification.arguments.map(argument => argument.type), return_type: specification.return_type,
        signature_evidence: specification.signature_evidence, module: module.path,
        bridge: specification.abi === 'swift' ? 'frida-swift' : 'frida-native'
      }};
    }
    function pointerValue(specification, pathBinding = null, rootLease = null) {
      if (specification.native_path !== undefined) {
        if (pathBinding === null) fail('native path requires an exact direct argument slot or investigation root');
        return resolveNativePath(specification.native_path, pathBinding).pointer;
      }
      if (specification.alias !== undefined) {
        if (nativeAliases.has(specification.alias)) return nativeAliases.get(specification.alias);
        const object = aliases.get(specification.alias);
        if (object !== undefined) return object.handle;
        fail('unknown/expired native pointer alias');
      }
      if (specification.objc !== undefined) {
        const object = receiverOf(specification.objc);
        if (object.$kind !== 'instance') fail('native object argument requires an instance');
        hold(object);
        if (rootLease !== null) registerNativePathOwner(rootLease, object);
        return object.handle;
      }
      if (specification.getter !== undefined) {
        const returned = callNative(specification.getter, false);
        if (returned.value.isNull()) fail('borrowed native getter returned null');
        holdPointer(returned.value);
        return returned.value;
      }
      if (specification.read !== undefined) {
        const recipe = specification.read;
        const resolved = moduleFor(recipe.module);
        const evidence = (payload.native_resources || {})[recipe.evidence.file];
        if (!evidence || evidence.sha256 !== recipe.evidence.sha256)
          fail('borrowed ivar layout evidence was not pinned before attachment');
        const receiver = receiverOf(recipe.receiver);
        if (receiver.$kind !== 'instance' || receiver.$className !== recipe.receiver_class)
          fail('borrowed ivar read did not reach its exact declared instance class');
        hold(receiver);
        if (rootLease !== null) registerNativePathOwner(rootLease, receiver);
        const metadata = ivarEncoding(receiver, recipe.field.name);
        if (metadata.class !== recipe.field.class || metadata.name !== recipe.field.name ||
            metadata.encoding !== recipe.field.encoding || metadata.module !== recipe.module.path)
          fail('borrowed ivar runtime owner/type/module differs from its frozen evidence');
        if (!recipe.field.encoding.replace(/^[rnNoORV]+/, '').startsWith('^'))
          fail('borrowed ivar is not an actual pointer slot');
        const offset = Number(metadata.offset);
        const instanceBytes = Number(ObjC.api.class_getInstanceSize(receiver.$class.handle));
        if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(instanceBytes) ||
            instanceBytes < Process.pointerSize || instanceBytes > 1024 * 1024 ||
            offset + Process.pointerSize > instanceBytes ||
            (recipe.field.offset !== undefined && recipe.field.offset !== offset))
          fail('borrowed ivar slot/instance bounds differ from the declared layout');
        const pointer = readable(receiver.handle.add(offset), Process.pointerSize).readPointer();
        if (rootLease !== null) rootLease.reads.push({slot: receiver.handle.add(offset), type: 'pointer', value: pointer});
        const identity = holdPointer(pointer);
        const observed = {recipe, receiver_class: receiver.$className, field: metadata,
          image_identity: resolved.identity, instance_bytes: instanceBytes, pointer: identity};
        pointerReads.push(observed);
        send({event: 'native-pointer-read', ...observed});
        if (pointer.isNull()) fail('borrowed ivar pointer is null; no nullable native ABI policy was declared');
        return pointer;
      }
      if (specification.swift_read !== undefined) return swiftFieldRead(specification.swift_read, true, rootLease).pointer;
      if (specification.callable !== undefined) return callablePointer(specification.callable).pointer;
      fail('native pointers require declared recipes, never raw addresses');
    }
    function ownSwiftStorage(value) {
      if (value === null || typeof value !== 'object') fail('Swift SDK return has no owned value object');
      const handle = Object.getOwnPropertyDescriptor(value, 'handle');
      const metadata = Object.getOwnPropertyDescriptor(value, '$metadata');
      if (!handle || !Object.prototype.hasOwnProperty.call(handle, 'value') ||
          !metadata || !Object.prototype.hasOwnProperty.call(metadata, 'value'))
        fail('Swift SDK return storage/metadata must be own data properties');
      return {handle: handle.value, metadata: metadata.value};
    }
    function allocationDescriptorEqual(left, right) {
      return left !== undefined && right !== undefined && left.value === right.value &&
        left.writable === right.writable && left.enumerable === right.enumerable &&
        left.configurable === right.configurable && left.get === right.get && left.set === right.set;
    }
    function swiftStorageLayout(metadata) {
      readable(metadata.handle, 16);
      return {witness: witnessLayout(metadata.handle),
        descriptor: readable(metadata.handle.add(8), 8).readPointer().toString()};
    }
    function verifySwiftStorage(value, type, wanted) {
      const record = sdkReturnStorage.get(value), actual = ownSwiftStorage(value);
      if (!record || record.bytes !== type.bytes || record.type !== JSON.stringify(type) ||
          record.metadata !== wanted || actual.metadata !== wanted ||
          wanted.handle.toString() !== record.metadataAddress || !actual.handle ||
          actual.handle.toString() !== record.address || record.owner.toString() !== record.address ||
          actual.handle.compare(record.owner) !== 0)
        fail('Swift POD storage is untracked, changed or outside its exact SDK allocation');
      if (!dataEqual(record.layout, swiftStorageLayout(wanted)))
        fail('Swift POD metadata/value witness changed after SDK storage capture');
      return record.owner;
    }
    function captureSwiftReturn(type, wanted, dispatch) {
      if (sdkAllocationCaptureOwner !== null) fail('Swift SDK allocation capture cannot be nested');
      const layout = swiftStorageLayout(wanted);
      const metadataAddress = wanted.handle.toString();
      if (layout.witness.metadata_kind !== 0x200 || layout.witness.stride !== type.bytes)
        fail('Swift SDK return differs from its validated POD stride');
      // Frida 17.23.3 core.js makes public alloc immutable and forwards its
      // default allocation to _alloc(size, 'rw', NULL, 0). Capture only that
      // backing call; public alloc and all target memory protections stay put.
      const memoryAPI = Memory;
      const publicAllocator = Object.getOwnPropertyDescriptor(memoryAPI, 'alloc');
      if (!publicAllocator || !Object.prototype.hasOwnProperty.call(publicAllocator, 'value') ||
          typeof publicAllocator.value !== 'function' || publicAllocator.writable !== false ||
          publicAllocator.configurable !== false)
        fail('trusted Swift SDK public allocator differs from its fixed immutable interface');
      const original = Object.getOwnPropertyDescriptor(memoryAPI, '_alloc');
      if (!original || !Object.prototype.hasOwnProperty.call(original, 'value') ||
          typeof original.value !== 'function' || original.writable !== true)
        fail('trusted Swift SDK allocator cannot be safely captured');
      const captured = [];
      const tracked = function (...args) {
        if (sdkAllocationCaptureOwner !== sdkCaptureToken || Memory !== memoryAPI || args.length !== 4 ||
            !Number.isSafeInteger(args[0]) || args[0] < 1 || args[0] > 16384 ||
            args[1] !== 'rw' || !args[2] || !args[2].isNull() || args[3] !== 0 ||
            sdkAllocationOwners.length >= 128 || sdkAllocationBytes + args[0] > 1024 * 1024)
          fail('Swift SDK allocation exceeds its exact owned-storage budget');
        const owner = Reflect.apply(original.value, memoryAPI, args);
        if (!owner || owner.isNull()) fail('trusted Swift SDK allocator returned no owner');
        const record = {owner, address: owner.toString(), bytes: args[0]};
        captured.push(record);
        sdkAllocationOwners.push(owner); // The original NativePointer owns its GC allocation.
        sdkAllocationBytes += args[0];
        return owner;
      };
      const installed = {...original, value: tracked};
      let value;
      sdkAllocationCaptureOwner = sdkCaptureToken;
      try {
        Object.defineProperty(memoryAPI, '_alloc', installed);
        if (!allocationDescriptorEqual(Object.getOwnPropertyDescriptor(memoryAPI, '_alloc'), installed))
          fail('trusted Swift SDK allocator capture did not install exactly');
        value = dispatch();
      } finally {
        const changed = Memory !== memoryAPI ||
          !allocationDescriptorEqual(Object.getOwnPropertyDescriptor(memoryAPI, '_alloc'), installed) ||
          !allocationDescriptorEqual(Object.getOwnPropertyDescriptor(memoryAPI, 'alloc'), publicAllocator);
        try {
          Object.defineProperty(memoryAPI, '_alloc', original);
          if (!allocationDescriptorEqual(Object.getOwnPropertyDescriptor(memoryAPI, '_alloc'), original))
            fail('trusted Swift SDK allocator restore did not match');
        } catch (error) {
          // Leave the script terminally blocked until its normal unload. A
          // restore failure must not permit any later native work or RPC.
          result.native_state_uncertain = true;
          fail('trusted Swift SDK allocator could not be restored: ' + error.message);
        }
        if (changed) {
          result.native_state_uncertain = true;
          fail('trusted Swift SDK allocator changed during the captured call');
        }
        sdkAllocationCaptureOwner = null;
      }
      const actual = ownSwiftStorage(value);
      const matches = actual.handle === undefined ? [] : captured.filter(record =>
        record.address === actual.handle.toString() && actual.handle.compare(record.owner) === 0);
      if (actual.metadata !== wanted || matches.length !== 1 || matches[0].bytes !== type.bytes)
        fail('Swift POD return is not the exact freshly allocated SDK storage/metadata');
      const record = {...matches[0], metadata: wanted, metadataAddress,
        type: JSON.stringify(type), layout};
      sdkReturnStorage.set(value, record);
      allocations.push(value);
      verifySwiftStorage(value, type, wanted);
      send({event: 'swift-sdk-storage-verified', bytes: type.bytes, allocation_bytes: record.bytes,
        ownership: 'frida_owned', expires: 'transaction'});
      return value;
    }
    function verifySwiftArguments(declarations, values, image) {
      declarations.forEach((argument, index) => {
        const type = argument.type;
        if (typeof type !== 'object') return;
        if (type.kind === 'swift_type') verifySwiftStorage(values[index], type, swiftType(type, image));
        else if (type.kind === 'struct') verifySwiftArguments(type.fields, values[index], image);
      });
    }
    function nativeArgument(argument, pathBinding = null) {
      const type = argument.type, value = argument.value;
      if (typeof type === 'object') {
        if (type.kind === 'struct')
          return type.fields.map(field => nativeArgument({type: field.type, value: value[field.name]}));
        if (type.kind === 'buffer') {
          const pointer = Memory.alloc(type.bytes);
          pointer.writeByteArray(type.direction === 'out' ? new Uint8Array(type.bytes) : value);
          allocations.push(pointer);
          return pointer;
        }
        if (type.kind === 'swift_type') {
          const wanted = swiftType(type, moduleFor(value.getter.target.module));
          const returned = callNative(value.getter, false).value;
          verifySwiftStorage(returned, type, wanted);
          return returned;
        }
        fail('unsupported aggregate value');
      }
      if (type === 'pointer') return pointerValue(value, pathBinding);
      if (type === 'utf8') {
        const pointer = Memory.allocUtf8String(value);
        allocations.push(pointer);
        return pointer;
      }
      if (type === 'int64' || type === 'long') return int64(value);
      if (type === 'uint64' || type === 'ulong') return uint64(value);
      // NativeFunction's C bool converter expects an integer. Keep logical
      // JSON values and ObjC argumentValue unchanged; direct Swift bool ABI
      // remains rejected by nativeType before arguments are resolved.
      if (type === 'bool') return value ? 1 : 0;
      return value;
    }
    function serializeNative(value, type, alias, image) {
      if (typeof type === 'object') {
        if (type.kind === 'struct') {
          if (!Array.isArray(value) || value.length !== type.fields.length) fail('native return aggregate layout differs');
          const fields = {};
          type.fields.forEach((field, index) => { fields[field.name] = serializeNative(value[index], field.type, undefined, image); });
          return fields;
        }
        if (type.kind === 'swift_type') {
          const wanted = swiftType(type, image);
          const owner = verifySwiftStorage(value, type, wanted);
          return {type, bytes: Array.from(new Uint8Array(owner.readVolatile(type.bytes))),
            ownership: 'trivial_pod', expires: 'transaction'};
        }
        fail('unsupported aggregate return');
      }
      if (type === 'pointer') return holdPointer(value, alias);
      if (type === 'utf8') {
        if (value.isNull()) return null;
        const range = Process.findRangeByAddress(value);
        if (range === null || !range.protection.includes('r')) fail('native UTF-8 return is unreadable');
        const available = Math.min(4097, Number(range.base.add(range.size).sub(value).toString()));
        const bytes = Array.from(new Uint8Array(value.readVolatile(available)));
        const end = bytes.indexOf(0);
        if (end < 0 || end > 4096) fail('native UTF-8 return has no terminator within its bound');
        return end === 0 ? '' : value.readUtf8String(end);
      }
      return serialize(value, type);
    }
    function callNative(specification, invocation, alias, rootCapture = null) {
      const readsBefore = pointerReads.length;
      const swiftReadsBefore = swiftFieldReads.length;
      const callableBefore = callableEntries.length;
      const pathsBefore = nativePaths.length;
      const pathCall = prepareNativePathCall(specification, invocation);
      const rootScope = rootCapture === null ? null : borrowedRootScopes.get(rootCapture);
      if (rootCapture !== null && (!rootScope || rootScope.specification !== specification || invocation ||
          alias !== undefined || pathCall !== null || rootScope.phase !== 'acquiring'))
        fail('borrowed root capture cannot be copied or used for another call');
      // Resolve exact code/prototype before arguments can invoke getter recipes.
      const found = nativeFunction(specification);
      if (rootScope !== null) rootScope.function = found.function_;
      const arguments_ = specification.arguments.map((argument, index) => nativeArgument(argument,
        pathCall === null ? null : {call: pathCall, argument: index}));
      if (pathCall !== null) bindNativePathCall(pathCall, found.function_, arguments_);
      // A later argument/getter must not invalidate an earlier POD argument.
      verifySwiftArguments(specification.arguments, arguments_, found.image);
      deadline();
      const dispatch = () => {
        if (pathCall === null && invocation) result.invocations++;
        send({event: invocation ? 'native-invoke-start' : 'native-getter-start', function: found.metadata});
        if (pathCall !== null) sealNativePaths(pathCall);
        return targetCall(found.function_, arguments_, pathCall, rootCapture);
      };
      const value = specification.abi === 'swift' && typeof specification.return_type === 'object' &&
        specification.return_type.kind === 'swift_type'
        ? captureSwiftReturn(specification.return_type, swiftType(specification.return_type, found.image), dispatch)
        : dispatch();
      const outBuffers = [];
      let completion = null;
      try {
        specification.arguments.forEach((argument, index) => {
          if (typeof argument.type === 'object' && argument.type.kind === 'buffer' && argument.type.direction !== 'in')
            outBuffers.push({argument: index, bytes: Array.from(new Uint8Array(arguments_[index].readVolatile(argument.type.bytes)))});
        });
        if (specification.compiler_adapter !== undefined) completion = compilerCompletion(specification, value, arguments_);
      } catch (error) {
        if (specification.compiler_adapter !== undefined) result.native_state_uncertain = true;
        throw error;
      }
      send({event: invocation ? 'native-invoke-returned' : 'native-getter-returned'});
      return {value, function: found.metadata, return_type: specification.return_type,
        serialized: invocation ? serializeNative(value, specification.return_type, alias, found.image) : null, out_buffers: outBuffers,
        pointer_reads: pointerReads.slice(readsBefore), swift_field_reads: swiftFieldReads.slice(swiftReadsBefore),
        callable_entries: callableEntries.slice(callableBefore), compiler_completion: completion,
        native_paths: nativePaths.slice(pathsBefore)};
    }
    function compilerCompletion(specification, returnedStatus, arguments_) {
      const adapter = specification.compiler_adapter, argument = adapter.completion_argument;
      const bytes = arguments_[argument].readVolatile(24), view = new DataView(bytes);
      const status = view.getUint32(0, true), dispatch = view.getUint32(4, true), uncertain = view.getUint32(8, true);
      const reserved = view.getUint32(12, true);
      const returned = Number(returnedStatus);
      if (adapter.family === 'cxx_owned_string_bytes') {
        const payloadArgument = 3, kind = specification.arguments[payloadArgument].type;
        if (argument !== 4 || specification.arguments.length !== 5 || typeof kind !== 'object' ||
            kind.kind !== 'buffer' || kind.direction !== 'out' ||
            !Number.isInteger(kind.bytes) || kind.bytes < 1 || kind.bytes > 16384)
          fail('owned return payload does not match its fixed compiler transport');
        const length = view.getUint32(16, true), reservedTail = view.getUint32(20, true);
        const payload = Array.from(new Uint8Array(arguments_[payloadArgument].readVolatile(kind.bytes)));
        const paddingZero = length <= kind.bytes && payload.slice(status === 0 ? length : 0).every(byte => byte === 0);
        const valid = [0, 1, 2, 3].includes(status) && Number.isInteger(returned) && returned === status &&
          [0, 1].includes(dispatch) && [0, 1].includes(uncertain) && reserved === 0 && reservedTail === 0 &&
          length <= kind.bytes && paddingZero &&
          (status !== 0 || dispatch === 1 && uncertain === 0) &&
          (status !== 1 || dispatch === 0 && uncertain === 0) &&
          (![2, 3].includes(status) || uncertain === 1) && (status !== 3 || dispatch === 1) &&
          (status === 0 || length === 0);
        if (!valid || uncertain === 1 || [2, 3].includes(status)) result.native_state_uncertain = true;
        const completion = {format: 'anyuse.rea-compiler-bytes-completion/1', family: adapter.family, argument, bytes: 24,
          evidence: adapter.evidence, valid, status, status_return: returned,
          dispatch_attempted: dispatch === 0 ? false : dispatch === 1 ? true : null,
          uncertain: uncertain === 0 ? false : uncertain === 1 ? true : null, reserved_zero: reserved,
          result_type: 'bytes', payload_argument: payloadArgument, payload_bytes: kind.bytes, length, length_offset: 16,
          reserved_tail_zero: reservedTail, padding_zero: paddingZero,
          value: length <= kind.bytes ? payload.slice(0, length) : null, original_body_entry_proved: false};
        send({event: 'compiler-adapter-completion', ...completion});
        return completion;
      }
      const value = view.getFloat64(16, true);
      const valid = [0, 1, 2, 3].includes(status) && Number.isInteger(returned) && returned === status &&
        [0, 1].includes(dispatch) && [0, 1].includes(uncertain) && reserved === 0 &&
        (status !== 0 || dispatch === 1 && uncertain === 0 && Number.isFinite(value)) &&
        (status !== 1 || dispatch === 0 && uncertain === 0) &&
        (![2, 3].includes(status) || uncertain === 1) && (status !== 3 || dispatch === 1);
      if (!valid || uncertain === 1 || [2, 3].includes(status)) result.native_state_uncertain = true;
      const completion = {format: 'anyuse.rea-compiler-completion/1', family: adapter.family, argument, bytes: 24,
        evidence: adapter.evidence, valid, status, status_return: returned,
        dispatch_attempted: dispatch === 0 ? false : dispatch === 1 ? true : null,
        uncertain: uncertain === 0 ? false : uncertain === 1 ? true : null, reserved_zero: reserved,
        value: Number.isFinite(value) ? value : null, original_body_entry_proved: false};
      send({event: 'compiler-adapter-completion', ...completion});
      return completion;
    }
    function loadHelper(operation) {
      const policy = payload.helper_policy;
      const requested = {module: operation.module, build_evidence: operation.build_evidence};
      if (!policy || !policy.policy || policy.mode !== 0o600 ||
          !dataEqual(policy.policy.target, {bundle: payload.bundle, fingerprint: payload.target, scope: 'owned_debug_target'}) ||
          !Array.isArray(policy.policy.helpers) || !policy.policy.helpers.some(helper => dataEqual(helper, requested)))
        fail('helper loading is not explicitly authorized for this exact debug target');
      const pin = (payload.native_resources || {})[operation.module.path];
      const evidence = (payload.native_resources || {})[operation.build_evidence.file];
      if (!pin || pin.sha256 !== operation.module.sha256 || !evidence || evidence.sha256 !== operation.build_evidence.sha256)
        fail('explicit helper code/build evidence is not pinned');
      const cpu = Process.arch === 'arm64' ? 0x0100000c : Process.arch === 'x64' ? 0x01000007 : null;
      if (cpu === null || !Array.isArray(pin.macho_images) || !pin.macho_images.some(image => image.cpu_type === cpu))
        fail('helper has no pinned image for the actual target CPU');
      const existing = Process.enumerateModules().filter(module => module.path === operation.module.path);
      if (existing.length > 1) fail('loaded helper identity is ambiguous; no reload attempted');
      let image;
      if (existing.length === 1) image = moduleFor(operation.module);
      else {
        deadline();
        const priorUncertainty = result.native_state_uncertain;
        result.helper_load_attempts++;
        const residency = {module: operation.module, state: 'unknown', remains_loaded: null};
        result.helper_residency.push(residency);
        // Constructors may run even when the loader throws. There is no safe
        // rollback/dlclose claim and no automatic replacement or retry.
        result.native_state_uncertain = true;
        send({event: 'explicit-helper-load-start', module: operation.module, policy_sha256: policy.sha256});
        const loaded = targetCall(() => Module.load(operation.module.path), []);
        if (!loaded || loaded.path !== operation.module.path) fail('loader returned a different helper; resource review required');
        imageCache.delete(operation.module.path);
        image = moduleFor(operation.module);
        residency.state = 'resident'; residency.remains_loaded = true;
        result.native_state_uncertain = priorUncertainty;
      }
      const observed = {module: operation.module, build_evidence: operation.build_evidence,
        image_identity: image.identity, module_size: image.extent, reported_module_size: image.module.size,
        already_loaded: existing.length === 1, new_load_attempted: existing.length === 0, remains_loaded: true,
        policy_file: policy.file, policy_sha256: policy.sha256, scope: 'owned_debug_target', process_pid: Process.id};
      send({event: 'explicit-helper-ready', ...observed});
      return observed;
    }
    function memoryField(pointer, field) {
      const address = pointer.add(field.offset);
      const widths = {bool: 1, int8: 1, uint8: 1, int16: 2, uint16: 2, int32: 4, uint32: 4,
        int64: 8, uint64: 8, long: Process.pointerSize, ulong: Process.pointerSize,
        float: 4, double: 8, pointer: Process.pointerSize};
      readable(address, widths[field.type]);
      const readers = {bool: 'readU8', int8: 'readS8', uint8: 'readU8', int16: 'readS16', uint16: 'readU16',
        int32: 'readS32', uint32: 'readU32', int64: 'readS64', uint64: 'readU64', long: 'readS64', ulong: 'readU64',
        float: 'readFloat', double: 'readDouble', pointer: 'readPointer'};
      return address[readers[field.type]]();
    }
    function pathCopy(value) { return JSON.parse(JSON.stringify(value)); }
    function pathFreeze(value) {
      if (value !== null && typeof value === 'object') {
        Object.values(value).forEach(pathFreeze);
        Object.freeze(value);
      }
      return value;
    }
    function pathLayoutContract(layout) {
      return Object.fromEntries(Object.entries(pathCopy(layout)).filter(([key]) => key !== 'evidence'));
    }
    function receiverHypothesis(recipe) { return recipe.format === RECEIVER_HYPOTHESIS_FORMAT; }
    function receiverHypothesisV2(recipe) { return recipe !== null && typeof recipe === 'object' && recipe.format === RECEIVER_HYPOTHESIS_V2_FORMAT; }
    function stripPacPointer(pointer, pacMode = 'none', key = 'da') {
      if (!pointer || pointer.isNull()) return pointer;
      if (pacMode === 'none') return pointer;
      if (pacMode !== 'arm64e_strip' || !['da', 'ia'].includes(key))
        fail('unsupported PAC strip mode or key');
      if (typeof pointer.strip !== 'function')
        fail('arm64e_strip requires Frida NativePointer.strip(key) support');
      const stripped = pointer.strip(key);
      if (!stripped || stripped.isNull()) fail('PAC strip produced null pointer');
      return stripped;
    }
    function resolveReceiverHypothesisV2Hop(currentPtr, hop) {
      if (!currentPtr || currentPtr.isNull() || !currentPtr.and(ptr(7)).isNull())
        fail('V2 receiver hop input pointer is null or misaligned');
      readable(currentPtr, 8);
      if (hop.kind === 'field_pointer') {
        const slot = currentPtr.add(hop.field_offset);
        if (!slot.and(ptr(7)).isNull()) fail('V2 field_pointer slot is misaligned');
        const next = readable(slot, 8).readPointer();
        if ((hop.non_null ?? true) && next.isNull()) fail('V2 field_pointer hop produced null pointer');
        if (!next.and(ptr(7)).isNull()) fail('V2 field_pointer hop produced misaligned pointer');
        readable(next, 8);
        return {pointer: next, slot, vectorBegin: null, vectorEnd: null, vectorCap: null};
      }
      if (hop.kind === 'array_element') {
        if ((hop.element_kind || 'pointer') !== 'pointer' || hop.stride % 8 !== 0)
          fail('V2 array_element hop requires 8-byte aligned pointer elements');
        const slot = currentPtr.add(hop.base_offset + hop.index * hop.stride);
        if (!slot.and(ptr(7)).isNull()) fail('V2 array_element slot is misaligned');
        const next = readable(slot, 8).readPointer();
        if ((hop.non_null ?? true) && next.isNull()) fail('V2 array_element hop produced null pointer');
        if (!next.and(ptr(7)).isNull()) fail('V2 array_element hop produced misaligned pointer');
        readable(next, 8);
        return {pointer: next, slot, vectorBegin: null, vectorEnd: null, vectorCap: null};
      }
      if (hop.kind === 'vector_element') {
        if ((hop.element_kind || 'pointer') !== 'pointer')
          fail('V2 vector_element hop requires pointer elements');
        const vecBase = currentPtr.add(hop.vector_offset);
        if (!vecBase.and(ptr(7)).isNull()) fail('V2 vector_element header is misaligned');
        readable(vecBase, 24);
        const begin = vecBase.readPointer();
        const end = vecBase.add(8).readPointer();
        const cap = vecBase.add(16).readPointer();
        if (begin.isNull() || end.isNull() || cap.isNull() ||
            !begin.and(ptr(7)).isNull() || !end.and(ptr(7)).isNull() || !cap.and(ptr(7)).isNull() ||
            end.compare(begin) <= 0 || cap.compare(end) < 0)
          fail('V2 vector_element hop encountered empty or invalid libc++ vector header');
        const spanBytes = Number(end.sub(begin).toString());
        const capBytes = Number(cap.sub(begin).toString());
        if (!Number.isSafeInteger(spanBytes) || !Number.isSafeInteger(capBytes) ||
            spanBytes < 8 || spanBytes % 8 !== 0 || capBytes < spanBytes || capBytes > 16 * 1024 * 1024)
          fail('V2 vector_element hop vector span is invalid or exceeds bounds');
        const count = spanBytes / 8;
        if (hop.index < 0 || hop.index >= count) fail('V2 vector_element index out of bounds');
        const elemSlot = begin.add(hop.index * 8);
        const next = readable(elemSlot, 8).readPointer();
        if ((hop.non_null ?? true) && next.isNull()) fail('V2 vector_element hop produced null pointer');
        if (!next.and(ptr(7)).isNull()) fail('V2 vector_element hop produced misaligned pointer');
        readable(next, 8);
        return {pointer: next, slot: elemSlot, vectorBegin: begin, vectorEnd: end, vectorCap: cap};
      }
      fail('unsupported V2 memory hop kind');
    }
    function verifyReceiverHypothesisV2VptrAndSlot(receiverPtr, prepared) {
      const recipe = prepared.recipe, vptrSpec = recipe.vptr, consumer = recipe.consumer;
      if (!receiverPtr || receiverPtr.isNull() || !receiverPtr.and(ptr(7)).isNull())
        fail('V2 terminal receiver pointer is null or misaligned');
      const subobjOffset = vptrSpec.subobject_offset || 0;
      const vptrFieldAddr = receiverPtr.add(subobjOffset);
      if (!vptrFieldAddr.and(ptr(7)).isNull()) fail('V2 receiver vptr slot is misaligned');
      const rawVptr = readable(vptrFieldAddr, 8).readPointer();
      const vptr = stripPacPointer(rawVptr, vptrSpec.pac_mode || 'none', 'da');
      if (!vptr || vptr.isNull() || !vptr.and(ptr(7)).isNull())
        fail('V2 receiver vptr is null or misaligned');
      const vtableBytes = consumer.dispatch === 'vtable_slot' ? consumer.vtable_slot_offset + 8 : 8;
      const expectedVptr = nativePathDataLocation(vptrSpec.image, vptrSpec.data_offset, vtableBytes, 8, prepared).address;
      if (vptr.compare(expectedVptr) !== 0)
        fail('V2 receiver runtime vptr differs from its pinned Mach-O data section offset');
      const expectedEntry = entryFor(consumer.entry, consumer.signature_evidence, consumer.abi).address;
      if (consumer.dispatch === 'vtable_slot') {
        const slotAddress = vptr.add(consumer.vtable_slot_offset);
        if (!slotAddress.and(ptr(7)).isNull()) fail('V2 vtable slot address is misaligned');
        const rawSlotTarget = readable(slotAddress, 8).readPointer();
        const slotTarget = stripPacPointer(rawSlotTarget, consumer.pac_mode || 'none', 'ia');
        if (!slotTarget || slotTarget.isNull() || slotTarget.compare(expectedEntry) !== 0)
          fail('V2 vtable slot target differs from the pinned consumer entry');
      }
      return {vptr, entryAddress: expectedEntry};
    }
    function pathVptrContractV2(vptr) {
      return {format: 'anyuse.rea-native-receiver-hypothesis-vptr/2',
        image: pathCopy(vptr.image), data_offset: vptr.data_offset,
        subobject_offset: vptr.subobject_offset || 0, pac_mode: vptr.pac_mode || 'none',
        evidence_basis: {...RECEIVER_HYPOTHESIS_V2_BASIS}};
    }
    function pathConsumerContractV2(consumer) {
      return {format: 'anyuse.rea-native-receiver-hypothesis-consumer/2',
        dispatch: consumer.dispatch, vtable_slot_offset: consumer.vtable_slot_offset ?? null,
        this_adjustment: consumer.this_adjustment, pac_mode: consumer.pac_mode || 'none',
        entry: pathCopy(consumer.entry), abi: consumer.abi,
        argument_types: pathCopy(consumer.argument_types), return_type: consumer.return_type,
        evidence_basis: {...RECEIVER_HYPOTHESIS_V2_BASIS}};
    }
    function receiverLayoutContract(layout) {
      return {...pathLayoutContract(layout), evidence_basis: {...RECEIVER_HYPOTHESIS_BASIS}};
    }
    function pathRootContract(recipe) {
      if (receiverHypothesisV2(recipe)) return {format: 'anyuse.rea-native-receiver-hypothesis-root/2',
        root: {getter: Object.fromEntries(Object.entries(pathCopy(recipe.root.getter))
          .filter(([key]) => key !== 'signature_evidence'))},
        hops: pathCopy(recipe.hops).map(hop => Object.fromEntries(Object.entries(hop)
          .filter(([key]) => key !== 'signature_evidence'))),
        pointer_policy: 'borrowed_no_escape',
        evidence_basis: {...RECEIVER_HYPOTHESIS_V2_BASIS}};
      if (receiverHypothesis(recipe)) return {format: 'anyuse.rea-native-receiver-hypothesis-root/1',
        root: {getter: Object.fromEntries(Object.entries(pathCopy(recipe.root.getter))
          .filter(([key]) => key !== 'signature_evidence'))},
        pointee_type: recipe.root_type, pointer_policy: 'borrowed_no_escape',
        evidence_basis: {...RECEIVER_HYPOTHESIS_BASIS}};
      const root = recipe.root.lifecycle === undefined ? recipe.root : {lifecycle:
        Object.fromEntries(Object.entries(pathCopy(recipe.root.lifecycle)).filter(([key]) => key !== 'evidence'))};
      return {format: 'anyuse.rea-native-pointer-root/1', root,
        pointee_type: recipe.root_type, pointer_policy: 'borrowed_no_escape'};
    }
    function pathLifetimeContract(recipe) {
      if (receiverHypothesisV2(recipe)) return {format: 'anyuse.rea-native-receiver-hypothesis-lifetime/2',
        root: pathRootContract(recipe), mode: RECEIVER_HYPOTHESIS_V2_MODE,
        queue: recipe.lifetime.queue || 'main', scope: 'single_immediate_transaction',
        expected_native_calls: recipe.lifetime.expected_native_calls,
        terminal: {op: 'native_invoke', consumer: pathConsumerContractV2(recipe.consumer)},
        evidence_basis: {...RECEIVER_HYPOTHESIS_V2_BASIS}};
      if (receiverHypothesis(recipe)) return {format: 'anyuse.rea-native-receiver-hypothesis-lifetime/1',
        root: pathRootContract(recipe), types: recipe.types.map(receiverLayoutContract), path: [],
        mode: RECEIVER_HYPOTHESIS_MODE, preconditions: [], queue: 'main', scope: 'single_immediate_use',
        pointer_policy: 'borrowed_no_escape', intervening_target_calls: 0, post_dispatch_access: false,
        target_call_epoch: 'root_return_equals_terminal_use', this_adjustment: 0,
        terminal: pathTerminalContract(recipe), evidence_basis: {...RECEIVER_HYPOTHESIS_BASIS}};
      const contract = {format: 'anyuse.rea-native-pointer-lifetime/1', root: pathRootContract(recipe),
        types: recipe.types.map(pathLayoutContract), path: recipe.path, mode: recipe.lifetime.mode,
        preconditions: recipe.preconditions, queue: 'main', scope: 'single_dispatch',
        pointer_policy: 'borrowed_no_escape', concurrent_invalidation: 'independently_excluded', native_child_retained: false};
      if (recipe.lifetime.mode === 'main_queue_borrowed_root') Object.assign(contract, {
        scope: 'single_immediate_use', root_acquisition: 'zero_arg_callback_free_getter',
        root_type_admission: 'primary_vptr_at_zero', intervening_target_calls: 0, post_dispatch_access: false,
        target_call_epoch: 'root_return_equals_terminal_use', live_initialized_primary_base: 'independently_established',
        this_adjustment: 0, owner_thread: 'main_queue_independently_established',
        construction_destruction: 'excluded_for_entire_scope', reentrant_event_loop: 'excluded_for_entire_scope',
        terminal: pathTerminalContract(recipe)});
      if (recipe.lifetime.mode === 'main_queue_observed_root') Object.assign(contract, {
        scope: 'same_rpc_single_immediate_use', root_acquisition: 'constructor_observed_private_slot',
        root_type_admission: 'primary_vptr_at_zero', observer: lifecycleObserverContract(recipe),
        intervening_target_calls: 0, post_dispatch_access: false,
        target_call_epoch: 'slot_acquisition_equals_terminal_use', live_initialized_primary_base: 'witnessed_normal_constructor_return',
        this_adjustment: 0, owner_thread: 'observed_constructor_and_use_on_macos_main',
        construction_destruction: 'excluded_until_terminal_use', reentrant_event_loop: 'excluded_for_entire_scope',
        terminal: pathTerminalContract(recipe)});
      return contract;
    }
    function lifecycleObserverContract(recipe) {
      const observer = Object.fromEntries(Object.entries(pathCopy(recipe.root.lifecycle)).filter(([key]) => key !== 'evidence'));
      return {format: 'anyuse.rea-native-lifecycle-observer-contract/1', observer,
        pointee_type: recipe.root_type, layout: pathLayoutContract(recipe.types[0]),
        construction_completion: 'witnessed_complete_object_normal_return', constructor_caller: 'exact_admitted_return_site',
        destruction_coverage: 'earliest_primary_entry_independently_established',
        owner_thread: 'constructor_and_use_match_observed_macos_main', invalidation_thread: 'main_only_independently_established',
        hook_policy: 'checked_online_original_entries', external_hook_removal: 'independently_excluded',
        selection: 'sole_live_at_observation_end', scope: 'same_rpc_single_immediate_use', pointer_policy: 'borrowed_no_escape'};
    }
    function canonicalJSON(value) {
      if (Array.isArray(value)) return '[' + value.map(canonicalJSON).join(',') + ']';
      if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort()
        .map(key => JSON.stringify(key) + ':' + canonicalJSON(value[key])).join(',') + '}';
      return JSON.stringify(value);
    }
    function lifecycleEntry(entry, evidence) {
      if (!entry || !dataEqual(Object.keys(entry).sort(), ['module', 'offset']) ||
          typeof entry.offset !== 'string' || !/^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(entry.offset) ||
          !Number.isSafeInteger(Number(entry.offset)) || Number(entry.offset) % 4 !== 0)
        fail('lifecycle observer requires an exact aligned original code entry');
      const found = entryFor(entry, evidence, 'default');
      const offset = Number(entry.offset), end = offset + 4, image = found.image;
      if (image.identity.cpu_type !== 0x0100000c || image.identity.cpu_subtype !== 0)
        fail('lifecycle observer requires original plain-arm64 code');
      const intersects = section => Number(section.offset) < end && Number(section.offset) + section.bytes > offset;
      const sections = image.sections.filter(intersects), pin = (payload.native_resources || {})[entry.module.path];
      const declared = pin && Array.isArray(pin.macho_sections) &&
        pin.macho_sections.filter(row => sameImage(row.image_identity, image.identity));
      const originals = declared && declared.length === 1 && Array.isArray(declared[0].sections) && declared[0].sections.filter(intersects);
      if (sections.length !== 1 || !originals || originals.length !== 1 || !dataEqual(sections[0], originals[0]))
        fail('lifecycle observer needs one exact pinned instruction section for the whole entry');
      const section = sections[0], start = Number(section.offset), sectionEnd = start + section.bytes;
      if (offset < start || end > sectionEnd || !(section.flags & 0x80000400) ||
          [1, 0x0c, 0x12].includes(section.flags & 0xff))
        fail('lifecycle observer instruction crosses a section or names zero-fill');
      const segments = image.segments.filter(segment => segment.segment === section.segment &&
        start >= Number(segment.offset) && sectionEnd <= Number(segment.offset) + segment.bytes);
      if (segments.length !== 1 || !(segments[0].initial_protection & 4))
        fail('lifecycle observer instruction needs one originally executable segment');
      const segment = segments[0], relative = start - Number(segment.offset), fileOffset = image.sectionFileOffsets.get(section);
      if (!Number.isSafeInteger(fileOffset) || relative + section.bytes > segment.file_bytes ||
          fileOffset < segment.file_offset || fileOffset - segment.file_offset !== relative ||
          fileOffset + section.bytes > segment.file_offset + segment.file_bytes)
        fail('lifecycle observer code section disagrees with original file backing');
      inImage(found.image, found.address, 4);
      return found.address;
    }
    function lifecycleSpecification(recipe) {
      const spec = recipe.root.lifecycle;
      if (!spec || !dataEqual(Object.keys(spec).sort(), ['constructor', 'destructors', 'event_limit', 'evidence',
          'format', 'generation_limit', 'observe_ms', 'owner_thread', 'selection']) ||
          spec.format !== 'anyuse.rea-native-lifecycle-root/1' ||
          spec.owner_thread !== 'observed_constructor_on_macos_main' || spec.selection !== 'sole_live_at_observation_end' ||
          !Number.isSafeInteger(spec.observe_ms) || spec.observe_ms < 100 || spec.observe_ms > 45000 ||
          !Number.isSafeInteger(spec.event_limit) || spec.event_limit < 8 || spec.event_limit > 64 ||
          !Number.isSafeInteger(spec.generation_limit) || spec.generation_limit < 1 || spec.generation_limit > 8 ||
          !spec.constructor || !dataEqual(Object.keys(spec.constructor).sort(),
            ['completion', 'entry', 'return_sites', 'this_adjustment', 'this_argument']) ||
          spec.constructor.this_argument !== 0 || spec.constructor.this_adjustment !== 0 ||
          spec.constructor.completion !== 'complete_object_normal_return' ||
          !Array.isArray(spec.constructor.return_sites) || spec.constructor.return_sites.length < 1 || spec.constructor.return_sites.length > 8 ||
          !Array.isArray(spec.destructors) || spec.destructors.length < 1 || spec.destructors.length > 8 ||
          spec.destructors.some(row => !row || !dataEqual(Object.keys(row).sort(), ['entry', 'this_adjustment', 'this_argument']) ||
            row.this_argument !== 0 || row.this_adjustment !== 0))
        fail('lifecycle observer requires its closed bounded reviewed primary-entry declaration');
      const claims = [spec.constructor.entry, ...spec.constructor.return_sites, ...spec.destructors.map(row => row.entry)];
      for (const file of new Set(claims.map(entry => entry.module.path))) imageCache.delete(file);
      const addresses = claims.map(entry => lifecycleEntry(entry, spec.evidence.claim));
      if (new Set(addresses.map(address => address.toString())).size !== addresses.length)
        fail('lifecycle observer code roles alias one loaded address');
      return {spec, constructor: addresses[0], returns: addresses.slice(1, 1 + spec.constructor.return_sites.length),
        destructors: addresses.slice(1 + spec.constructor.return_sites.length)};
    }
    function assertObservedTerminal(recipe, binding, source) {
      const layout = recipe.types[0], identity = layout.runtime_identity;
      if (request.operations.length !== 1 || activeOperationIndex !== 0 || !recipe.root.lifecycle ||
          recipe.path.length !== 0 || recipe.types.length !== 1 || recipe.root_type !== recipe.result_type ||
          layout.name !== recipe.root_type || !identity || identity.kind !== 'pinned_data_vptr' ||
          pathField(layout, identity.field).offset !== 0 ||
          layout.fields.some(field => field.target !== undefined ||
            !borrowedRootScalars.has(field.type) && !(field.type === 'pointer' && field.name === identity.field)))
        fail('observed root requires one zero-edge primary-vptr scalar-only terminal operation');
      if (binding.call !== undefined) {
        const call = nativePathCalls.get(binding.call), operation = call && call.specification;
        if (operation && operation.op === 'qt_moc_invoke') {
          if (!call.qtMoc || !call.invocation || operation !== activeOperation || binding.argument !== 0 ||
              !operation.receiver || operation.receiver.native_path !== source || !recipe.consumer ||
              recipe.consumer.kind !== 'qt_moc' || recipe.consumer.pointee_type !== layout.name ||
              layout.fields.length !== 1 || !dataEqual(recipe.preconditions, []) ||
              !dataEqual(recipe.consumer.selection, operation.selection))
            fail('observed Qt receiver needs its dedicated fixed callback and original operation');
          qtMocConsumerContract(recipe.consumer);
        } else if (!call || !call.invocation || operation !== activeOperation || operation.op !== 'native_invoke' ||
            binding.argument !== 0 || !recipe.consumer || recipe.consumer.role !== 'cxx_this' ||
            recipe.consumer.argument !== 0 || operation.alias !== undefined || operation.arguments.length < 1 ||
            !(operation.return_type === 'void' || borrowedRootScalars.has(operation.return_type)) ||
            operation.arguments.slice(1).some(argument => !borrowedRootScalarValue(argument)))
          fail('observed root dispatch requires one direct unadjusted this and literal scalar tail');
      } else {
        const operation = binding.operation, allowed = borrowedRootScalarFields(recipe);
        if (recipe.consumer !== undefined || operation !== activeOperation || !operation || operation.op !== 'native_read' ||
            !dataEqual(Object.keys(operation).sort(), ['bytes', 'fields', 'layout_evidence', 'op', 'receiver']) ||
            !dataEqual(Object.keys(operation.receiver), ['native_path']) || operation.receiver.native_path !== source ||
            operation.bytes !== layout.bytes || !Array.isArray(operation.fields) || !operation.fields.length)
          fail('observed root read requires its single frozen scalar projection');
        let previous = -1;
        for (const field of operation.fields) {
          const index = allowed.findIndex(candidate => dataEqual(candidate, field));
          if (index <= previous) fail('observed root read changed its admitted scalar projection');
          previous = index;
        }
      }
    }
    function assertObservedCurrent(prepared) {
      const observer = lifecycleObserver;
      assertNativePathCurrent();
      if (!observer || observer.phase !== 'using' || observer.poisoned || observer.frames.length || observer.callbacks ||
          observer.owner !== Process.getCurrentThreadId() || !observer.selected || observer.selected.state !== 'live' ||
          observer.live().length !== 1 || observer.live()[0] !== observer.selected ||
          observer.epoch !== targetCallEpoch || observer.epoch !== 0 ||
          !dataEqual(prepared.recipe, observer.recipe) || activeOperation !== observer.operation ||
          !dataEqual(activeOperation, observer.operationSnapshot) || observer.consumed)
        fail('observed root generation/owner/phase/epoch expired; no replacement selection');
      observer.assertLocations();
      return observer;
    }
    function consumeObservedRoot(prepared, use) {
      const observer = assertObservedCurrent(prepared);
      if (!observer.scope || observer.scope.use !== use) fail('observed root use is not bound to this terminal');
      observer.scope.checked_epoch = targetCallEpoch;
      observer.scope.consume_event = observer.event('consume', observer.selected.ordinal, null, null, observer.owner);
      observer.scope.consumed = true;
      observer.consumed = true;
      observer.phase = 'consumed';
    }
    function qtMocSelection(selection) {
      const keys = ['format', 'module', 'qt_core', 'catalog', 'code', 'table', 'qt_sources',
        'class_name', 'record_offset', 'dispatcher_offset', 'local_index', 'signature'].sort();
      if (!selection || !dataEqual(Object.keys(selection).sort(), keys) ||
          selection.format !== 'anyuse.rea-qt-moc-selection/1' ||
          typeof selection.class_name !== 'string' || !selection.class_name.length || selection.class_name.length > 256 ||
          typeof selection.signature !== 'string' || !selection.signature.endsWith('()') || selection.signature.length > 1024 ||
          !Number.isSafeInteger(selection.local_index) || selection.local_index < 0 || selection.local_index > 511)
        fail('Qt dispatch needs its closed zero-argument class-local selection');
      for (const key of ['module', 'qt_core']) {
        const row = selection[key];
        if (!row || !dataEqual(Object.keys(row).sort(), ['path', 'sha256']) ||
            typeof row.path !== 'string' || !row.path.startsWith('/') ||
            typeof row.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.sha256))
          fail('Qt dispatch needs exact original image pins');
      }
      for (const key of ['catalog', 'code', 'table', 'qt_sources']) {
        const row = selection[key];
        if (!row || !dataEqual(Object.keys(row).sort(), ['file', 'sha256']) ||
            typeof row.file !== 'string' || !row.file.startsWith('/') ||
            typeof row.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.sha256))
          fail('Qt dispatch needs exact original catalog/code/table/source pins');
      }
      for (const key of ['record_offset', 'dispatcher_offset']) {
        const value = selection[key], alignment = key === 'record_offset' ? 8 : 4;
        if (typeof value !== 'string' || !/^0x(?:0|[1-9a-f][0-9a-f]{0,12})$/.test(value) ||
            !Number.isSafeInteger(Number(value)) || Number(value) % alignment !== 0)
          fail('Qt dispatch needs aligned canonical original record/code offsets');
      }
      return selection;
    }
    function qtMocConsumerContract(consumer) {
      if (!consumer || !dataEqual(Object.keys(consumer).sort(),
          ['kind', 'selection', 'argument', 'pointee_type', 'role', 'dispatch', 'this_adjustment', 'evidence'].sort()) ||
          consumer.kind !== 'qt_moc' || consumer.argument !== 0 || consumer.role !== 'qobject_primary_base' ||
          consumer.dispatch !== 'static_metacall' || consumer.this_adjustment !== 0)
        fail('Qt receiver requires its closed independent primary-QObject consumer');
      const selection = qtMocSelection(consumer.selection);
      if (consumer.pointee_type !== selection.class_name)
        fail('Qt declaring class differs from the admitted primary receiver');
      return {format: 'anyuse.rea-qt-moc-consumer/1', selection,
        callable: {entry: {module: selection.module, offset: selection.dispatcher_offset}, abi: 'default',
          argument_types: ['pointer', 'int32', 'int32', 'pointer'], return_type: 'void',
          context: 'none', pointer_policy: 'borrowed_no_escape'}, argument: 0, pointee_type: consumer.pointee_type,
        role: 'qobject_primary_base', dispatch: 'static_metacall', this_adjustment: 0,
        qobject_base: 'independently_admitted_primary_base_at_zero', call: 0,
        argv: {bytes: 8, slots: 1, slot0: 'null', ownership: 'private_no_escape', expires: 'call'}};
    }
    function qtMocRuntimePin(operation) {
      if (!operation || !dataEqual(Object.keys(operation).sort(), ['op', 'receiver', 'selection']) ||
          operation.op !== 'qt_moc_invoke' || !operation.receiver ||
          !dataEqual(Object.keys(operation.receiver), ['native_path']))
        fail('Qt invocation cannot accept public arguments, pointers or helpers');
      const selection = qtMocSelection(operation.selection), recipe = operation.receiver.native_path;
      const observed = recipe && recipe.lifetime && recipe.lifetime.mode === 'main_queue_observed_root';
      const borrowed = recipe && recipe.lifetime && recipe.lifetime.mode === 'main_queue_borrowed_root';
      if (!recipe || !recipe.lifetime || !(observed || borrowed) ||
          !recipe.root || !(observed ? recipe.root.lifecycle : recipe.root.getter) || !Array.isArray(recipe.path) || recipe.path.length !== 0 ||
          !Array.isArray(recipe.types) || recipe.types.length !== 1 || recipe.root_type !== recipe.result_type ||
          !recipe.types[0] || !Array.isArray(recipe.types[0].fields) || recipe.types[0].fields.length !== 1 ||
          !dataEqual(recipe.preconditions, []) ||
          !recipe.consumer || recipe.consumer.kind !== 'qt_moc' ||
          !dataEqual(recipe.consumer.selection, selection))
        fail('Qt invocation needs its one fresh observed primary receiver');
      const contract = qtMocConsumerContract(recipe.consumer);
      const resource = (payload.native_resources || {})[selection.catalog.file];
      const matches = resource && resource.sha256 === selection.catalog.sha256 &&
        Array.isArray(resource.qt_moc_dispatches) && resource.qt_moc_dispatches.filter(row =>
          row.format === 'anyuse.rea-qt-moc-runtime-pin/1' && dataEqual(row.selection, selection) &&
          dataEqual(row.consumer, contract));
      if (!matches || matches.length !== 1) fail('Qt catalog selection was not independently admitted by the worker');
      const pin = pathFreeze(pathCopy(matches[0])), method = pin.method;
      if (!method || method.class_name !== selection.class_name || method.local_index !== selection.local_index ||
          method.signature !== selection.signature || method.kind !== 'slot' || method.return_type !== 'void' ||
          !dataEqual(method.parameters, []))
        fail('Qt runtime pin does not name the selected ordinary void slot');
      return pin;
    }
    function qtMocRangeAddress(row) {
      if (!row || !dataEqual(Object.keys(row).sort(),
          ['module', 'offset', 'bytes', 'sha256', 'original_hex', 'kind'].sort()) ||
          !['data', 'code'].includes(row.kind) || !Number.isSafeInteger(row.bytes) ||
          row.bytes < 1 || row.bytes > 16384 || typeof row.sha256 !== 'string' ||
          !/^[a-f0-9]{64}$/.test(row.sha256) || typeof row.original_hex !== 'string' ||
          row.original_hex.length !== row.bytes * 2 || !/^[a-f0-9]+$/.test(row.original_hex) ||
          typeof row.offset !== 'string' || !/^0x(?:0|[1-9a-f][0-9a-f]{0,12})$/.test(row.offset) ||
          !Number.isSafeInteger(Number(row.offset)))
        fail('Qt original code/data range is malformed or outside its bound');
      if (row.kind === 'data') return nativePathDataLocation(row.module, row.offset, row.bytes, 1,
        {resources: new Map(Object.entries(payload.native_resources || {}))}).address;
      const image = moduleFor(row.module), offset = Number(row.offset), end = offset + row.bytes;
      if (offset % 4 || row.bytes % 4 || !Number.isSafeInteger(end)) fail('Qt code range must contain complete arm64 instructions');
      const address = image.module.base.add(row.offset), resource = (payload.native_resources || {})[row.module.path];
      const overlaps = section => Number(section.offset) < end && Number(section.offset) + section.bytes > offset;
      const sections = image.sections.filter(overlaps);
      const declared = resource && Array.isArray(resource.macho_sections) &&
        resource.macho_sections.filter(value => sameImage(value.image_identity, image.identity));
      const originals = declared && declared.length === 1 && declared[0].sections.filter(overlaps);
      if (sections.length !== 1 || !originals || originals.length !== 1 || !dataEqual(sections[0], originals[0]))
        fail('Qt code range crosses or lacks its exact original instruction section');
      const section = instructionFor(image, address, resource), start = Number(section.offset), finish = start + section.bytes;
      if (offset < start || end > finish || [1, 12, 18].includes(section.flags & 255))
        fail('Qt code range exceeds its original file-backed instruction section');
      const segments = image.segments.filter(segment => segment.segment === section.segment &&
        start >= Number(segment.offset) && finish <= Number(segment.offset) + segment.bytes);
      if (segments.length !== 1 || !(segments[0].initial_protection & 4)) fail('Qt code range has no original executable segment');
      const segment = segments[0], file = image.sectionFileOffsets.get(section), relative = start - Number(segment.offset);
      if (!Number.isSafeInteger(file) || relative + section.bytes > segment.file_bytes ||
          file < segment.file_offset || file - segment.file_offset !== relative ||
          file + section.bytes > segment.file_offset + segment.file_bytes)
        fail('Qt instruction section disagrees with original file backing');
      const mapping = Process.findRangeByAddress(address);
      if (mapping === null || !mapping.protection.includes('x')) fail('Qt instruction mapping is not executable');
      return inImage(image, address, row.bytes);
    }
    function assertQtMocRuntimeCurrent(pin) {
      assertNativePathCurrent();
      const selection = qtMocSelection(pin.selection);
      for (const key of ['module', 'qt_core']) {
        imageCache.delete(selection[key].path);
        const image = moduleFor(selection[key]);
        if (image.identity.cpu_type !== 0x0100000c || image.identity.cpu_subtype !== 0)
          fail('Qt callback requires actual pinned plain-arm64 images');
      }
      for (const key of ['catalog', 'code', 'table', 'qt_sources']) {
        const claim = selection[key], resource = (payload.native_resources || {})[claim.file];
        if (!resource || resource.file !== claim.file || resource.sha256 !== claim.sha256)
          fail('Qt original evidence resource changed or is absent');
      }
      const resource = payload.native_resources[selection.catalog.file];
      const matches = Array.isArray(resource.qt_moc_dispatches) && resource.qt_moc_dispatches.filter(row =>
        row.format === pin.format && dataEqual(row.selection, selection));
      if (!matches || matches.length !== 1 || !dataEqual(matches[0], pin))
        fail('Qt independently enriched runtime pin changed');
      if (!Array.isArray(pin.ranges) || pin.ranges.length < 1 || pin.ranges.length > 256 ||
          pin.ranges.reduce((sum, row) => sum + row.bytes, 0) > 128 * 1024)
        fail('Qt code/data comparison exceeds its fixed byte budget');
      for (const row of pin.ranges) {
        if (!dataEqual(row.module, selection.module)) fail('Qt initial callback range must belong to its selected original image');
        const address = qtMocRangeAddress(row), bytes = address.readByteArray(row.bytes);
        const hex = Array.from(new Uint8Array(bytes)).map(value => value.toString(16).padStart(2, '0')).join('');
        if (hex !== row.original_hex || Checksum.compute('sha256', bytes) !== row.sha256)
          fail('Qt original loaded code/data bytes drifted');
      }
      if (!Array.isArray(pin.record_pointers) || pin.record_pointers.length !== 3)
        fail('Qt callback requires its exact consumed record pointer slots');
      const selectedImage = moduleFor(selection.module);
      pin.record_pointers.forEach((row, index) => {
        const wantedSlot = '0x' + (Number(selection.record_offset) + (index + 1) * 8).toString(16);
        if (!row || !dataEqual(Object.keys(row).sort(), ['slot_offset', 'target_offset']) ||
            row.slot_offset !== wantedSlot || typeof row.target_offset !== 'string' ||
            !/^0x(?:0|[1-9a-f][0-9a-f]{0,12})$/.test(row.target_offset) ||
            !Number.isSafeInteger(Number(row.target_offset)) ||
            index === 2 && row.target_offset !== selection.dispatcher_offset)
          fail('Qt original record pointer roles changed');
        const slot = nativePathDataLocation(selection.module, row.slot_offset, 8, 8,
          {resources: new Map(Object.entries(payload.native_resources || {}))}).address;
        const actual = slot.readPointer(), expected = selectedImage.module.base.add(row.target_offset);
        inImage(selectedImage, expected, index === 2 ? 4 : 1);
        if (actual.compare(expected) !== 0) fail('Qt runtime record pointer differs from the original class catalog');
      });
    }
    function prepareQtMocCall(operation) {
      assertNativePathCurrent();
      if (activeOperation !== operation || activeOperationIndex !== 0 || request.operations.length !== 1 ||
          targetCallEpoch !== 0 || qtMocOperationTokens.has(operation))
        fail('Qt callback preparation cannot be repeated or enter an existing call epoch');
      const pin = qtMocRuntimePin(operation);
      assertQtMocRuntimeCurrent(pin);
      const consumer = operation.receiver.native_path.consumer;
      const internal = {target: {module: operation.selection.module, offset: operation.selection.dispatcher_offset},
        abi: 'default', arguments: [{type: 'pointer'}, {type: 'int32'}, {type: 'int32'}, {type: 'pointer'}],
        return_type: 'void', signature_evidence: consumer.evidence.claim};
      const found = nativeFunction(internal);
      // The only argv is private native storage. Caller JSON supplies no pointer.
      const vector = Memory.alloc(8);
      if (vector.isNull() || !vector.and(ptr(7)).isNull()) fail('Qt private argv allocation is not pointer-aligned');
      vector.writePointer(ptr(0));
      const token = Object.freeze({});
      const qtMoc = {pin, vector, expired: false, found, resources: new Map(), location: {
        address: ptr(found.address.toString()), base: ptr(found.image.module.base.toString()),
        identity: pathFreeze(pathCopy(found.image.identity))}, images: []};
      for (const claim of [operation.selection.module, operation.selection.qt_core]) {
        const image = moduleFor(claim);
        qtMoc.images.push({claim: pathFreeze(pathCopy(claim)), base: ptr(image.module.base.toString()),
          identity: pathFreeze(pathCopy(image.identity))});
      }
      for (const key of ['module', 'qt_core', 'catalog', 'code', 'table', 'qt_sources']) {
        const claim = operation.selection[key], name = key === 'module' || key === 'qt_core' ? claim.path : claim.file;
        qtMoc.resources.set(name, pathFreeze(pathCopy(payload.native_resources[name])));
      }
      const call = {specification: operation, snapshot: pathFreeze(pathCopy(operation)),
        argumentObjects: [operation.receiver], invocation: true, prepared: new Map(), leases: [], permits: [],
        sealAttempted: false, dispatchAttempted: false, function: null, arguments: null, qtMoc};
      nativePathCalls.set(token, call);
      qtMocOperationTokens.set(operation, token);
      call.prepared.set(0, prepareNativePath(operation.receiver.native_path, {call: token, argument: 0}));
      return token;
    }
    function assertQtMocCallCurrent(call) {
      if (!call || !call.qtMoc || call.qtMoc.expired || !call.qtMoc.vector ||
          !call.qtMoc.vector.readPointer().isNull() || !dataEqual(call.specification, call.snapshot))
        fail('Qt private argument vector or fixed call expired or changed');
      for (const [name, wanted] of call.qtMoc.resources)
        if (!dataEqual(payload.native_resources[name], wanted)) fail('Qt original resource closure changed within this dispatch');
      assertQtMocRuntimeCurrent(call.qtMoc.pin);
      const selection = call.specification.selection;
      // Recheck the captured callable; a matching reload is never adopted.
      imageCache.delete(selection.module.path);
      const current = entryFor({module: selection.module, offset: selection.dispatcher_offset},
        call.specification.receiver.native_path.consumer.evidence.claim, 'default');
      if (current.address.compare(call.qtMoc.location.address) !== 0 ||
          current.image.module.base.compare(call.qtMoc.location.base) !== 0 ||
          !dataEqual(current.image.identity, call.qtMoc.location.identity))
        fail('Qt original callback location changed; cached callable cannot follow a reload');
      for (const original of call.qtMoc.images) {
        imageCache.delete(original.claim.path);
        const current = moduleFor(original.claim);
        if (current.module.base.compare(original.base) !== 0 || !dataEqual(current.identity, original.identity))
          fail('Qt selected image location changed; no module reload adoption');
      }
    }
    function callQtMoc(operation) {
      const token = qtMocOperationTokens.get(operation), call = token && nativePathCalls.get(token);
      assertQtMocCallCurrent(call);
      if (call.specification !== operation || call.dispatchAttempted || call.leases.length ||
          activeOperation !== operation || activeOperationIndex !== 0)
        fail('Qt invocation lost its sole prepared operation');
      const observation = {format: 'anyuse.rea-qt-moc-observation/1', selection: pathCopy(operation.selection),
        consumer: qtMocConsumerContract(operation.receiver.native_path.consumer), call: 0,
        local_index: operation.selection.local_index,
        argv: {bytes: 8, slots: 1, slot0: 'null', ownership: 'private_no_escape', expires: 'call', expired: false},
        permit_consumed: false, returned: false, expires: 'transaction'};
      try {
        const receiver = resolveNativePath(operation.receiver.native_path, {call: token, argument: 0}).pointer;
        const arguments_ = [receiver, 0, operation.selection.local_index, call.qtMoc.vector];
        bindNativePathCall(token, call.qtMoc.found.function_, arguments_);
        deadline();
        send({event: 'native-invoke-start', function: call.qtMoc.found.metadata});
        sealNativePaths(token);
        assertQtMocCallCurrent(call);
        targetCall(call.qtMoc.found.function_, arguments_, token);
        observation.permit_consumed = call.permits.length === 1 && nativePathPermits.get(call.permits[0]).used === true;
        observation.returned = true;
        send({event: 'native-invoke-returned'});
        return {function: call.qtMoc.found.metadata, return_type: 'void', serialized: null,
          out_buffers: [], pointer_reads: [], swift_field_reads: [], callable_entries: [], compiler_completion: null,
          qt_moc: observation};
      } finally {
        call.qtMoc.vector = null;
        call.qtMoc.expired = true;
        observation.argv.expired = true;
      }
    }
    function pathTerminalContract(recipe) {
      if (recipe.consumer === undefined) return {op: 'native_read', bytes: recipe.types[0].bytes,
        fields: borrowedRootScalarFields(recipe)};
      return {op: recipe.consumer.kind === 'qt_moc' ? 'qt_moc_invoke' : 'native_invoke',
        consumer: pathConsumerContract(recipe.consumer, receiverHypothesis(recipe))};
    }
    function pathConsumerContract(consumer, hypothesis = false) {
      if (consumer.kind === 'qt_moc') return qtMocConsumerContract(consumer);
      const call = consumer.call, entry = {module: call.module};
      if (call.symbol !== undefined) entry.symbol = call.symbol;
      if (call.offset !== undefined) entry.offset = call.offset;
      const contract = {format: 'anyuse.rea-native-pointer-consumer/1', callable: {entry, abi: call.abi,
        argument_types: call.argument_types, return_type: call.return_type, context: call.context,
        pointer_policy: call.pointer_policy}, argument: consumer.argument, pointee_type: consumer.pointee_type,
        role: consumer.role, dispatch: consumer.dispatch, this_adjustment: consumer.this_adjustment};
      if (hypothesis) Object.assign(contract, {format: 'anyuse.rea-native-receiver-hypothesis-consumer/1',
        evidence_basis: {...RECEIVER_HYPOTHESIS_BASIS}});
      return contract;
    }
    function validArm64Subtype(subtype, allowArm64e = false) {
      return allowArm64e ? [0, 2].includes(subtype & 0xffffff) : subtype === 0;
    }
    function v2AllowsArm64e(recipe) {
      return receiverHypothesisV2(recipe) &&
        Boolean(recipe.vptr && recipe.vptr.pac_mode === 'arm64e_strip' &&
                recipe.consumer && (recipe.consumer.dispatch !== 'vtable_slot' || recipe.consumer.pac_mode === 'arm64e_strip'));
    }
    function assertNativePathCurrent(allowArm64e = false) {
      deadline();
      if (!nativePathActive || nativePathQueueOwner !== nativePathTransaction ||
          Process.id !== payload.pid || Process.mainModule.path !== payload.executable ||
          Process.platform !== 'darwin' || Process.arch !== 'arm64' || Process.pointerSize !== 8)
        fail('native path transaction/main-queue/plain-arm64 lifetime is not current');
      // A plain-arm64 claimed dylib does not establish the process's pointer
      // ABI. Inspect the actual executable even when it is not a path image.
      const main = Process.mainModule;
      imageCache.delete(main.path);
      const identity = imageFor(main).identity;
      if (identity.cpu_type !== 0x0100000c || !validArm64Subtype(identity.cpu_subtype, allowArm64e))
        fail('native path requires the actual plain-arm64 main image; arm64e/PAC is unsupported');
    }
    function pathField(layout, name) {
      const fields = layout.fields.filter(field => field.name === name);
      if (fields.length !== 1) fail('native path needs one exact declared own field');
      return fields[0];
    }
    function assertPathConsumer(recipe, call, argument) {
      const consumer = recipe.consumer;
      if (receiverHypothesisV2(recipe)) {
        const dispatch = call.dispatch || 'direct';
        if (!consumer || call.op !== 'native_invoke' || call.abi !== 'default' ||
            call.context !== undefined || call.compiler_adapter !== undefined ||
            argument !== 0 || dispatch !== consumer.dispatch ||
            (dispatch === 'vtable_slot' ? call.vtable_slot_offset !== consumer.vtable_slot_offset :
              call.vtable_slot_offset !== undefined && call.vtable_slot_offset !== null) ||
            !dataEqual(consumer.entry, call.target) || consumer.abi !== call.abi ||
            !dataEqual(consumer.argument_types, call.arguments.map(item => item.type)) ||
            consumer.return_type !== call.return_type ||
            !dataEqual(consumer.signature_evidence, call.signature_evidence))
          fail('V2 native receiver consumer differs from the actual terminal invocation');
        return;
      }
      if (consumer && consumer.kind === 'qt_moc') {
        if (call.op !== 'qt_moc_invoke' || argument !== 0 || !call.receiver ||
            call.receiver.native_path === undefined || recipe.result_type !== consumer.pointee_type ||
            !dataEqual(consumer.selection, call.selection))
          fail('Qt receiver must belong to its exact dedicated terminal operation');
        qtMocConsumerContract(consumer);
        return;
      }
      if (!consumer || call.abi !== 'default' || call.context !== undefined || call.compiler_adapter !== undefined ||
          consumer.argument !== argument || consumer.pointee_type !== recipe.result_type ||
          !['borrowed_pointer', 'cxx_this'].includes(consumer.role) || consumer.dispatch !== 'direct' ||
          consumer.this_adjustment !== 0 || consumer.role === 'cxx_this' && argument !== 0)
        fail('native path consumer is not its exact ordinary direct argument slot');
      const actual = {entry: call.target, abi: call.abi, argument_types: call.arguments.map(item => item.type),
        return_type: call.return_type, context: 'none', pointer_policy: 'borrowed_no_escape'};
      if (!dataEqual(pathConsumerContract(consumer, receiverHypothesis(recipe)).callable, actual))
        fail('native path consumer differs from the actual callable declaration');
    }
    const borrowedRootScalars = new Set(['bool', 'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32',
      'int64', 'uint64', 'long', 'ulong', 'float', 'double']);
    function borrowedRootScalarFields(recipe) {
      return recipe.types[0].fields.filter(field => borrowedRootScalars.has(field.type))
        .map(field => ({name: field.name, type: field.type, offset: field.offset}));
    }
    function borrowedRootOccurrences(value) {
      if (value === null || typeof value !== 'object') return 0;
      if (!Array.isArray(value) && Object.keys(value).length === 1 && value.native_path &&
          ['anyuse.rea-native-pointer-path/1', RECEIVER_HYPOTHESIS_FORMAT, RECEIVER_HYPOTHESIS_V2_FORMAT].includes(value.native_path.format))
        return value.native_path.lifetime &&
          ['main_queue_borrowed_root', RECEIVER_HYPOTHESIS_MODE, RECEIVER_HYPOTHESIS_V2_MODE].includes(value.native_path.lifetime.mode) ? 1 : 0;
      return Object.values(value).reduce((count, child) => count + borrowedRootOccurrences(child), 0);
    }
    function borrowedRootScalarValue(argument) {
      if (!borrowedRootScalars.has(argument.type)) return false;
      if (argument.type === 'bool') return typeof argument.value === 'boolean';
      if (['int64', 'uint64', 'long', 'ulong'].includes(argument.type))
        return typeof argument.value === 'string' && /^-?(0|[1-9][0-9]{0,19})$/.test(argument.value);
      return typeof argument.value === 'number' && Number.isFinite(argument.value) &&
        (['float', 'double'].includes(argument.type) || Number.isInteger(argument.value));
    }
    function assertBorrowedRootTerminal(recipe, binding, source) {
      const getter = recipe.root.getter, layout = recipe.types[0], identity = layout.runtime_identity;
      if (!getter || getter.kind !== 'native_function' || recipe.path.length !== 0 || recipe.types.length !== 1 ||
          recipe.root_type !== recipe.result_type || layout.name !== recipe.root_type ||
          !identity || identity.kind !== 'pinned_data_vptr' ||
          pathField(layout, identity.field).offset !== 0 ||
          layout.fields.some(field => field.target !== undefined ||
            !borrowedRootScalars.has(field.type) && !(field.type === 'pointer' && field.name === identity.field)) ||
          !activeOperation || activeOperationIndex !== request.operations.length - 1 ||
          borrowedRootOccurrences(request) !== 1 || request.operations.some(operation => operation.op === 'load_helper'))
        fail('borrowed root requires one terminal primary-vptr scalar-only scope');
      if (binding.call !== undefined) {
        const call = nativePathCalls.get(binding.call), operation = call && call.specification;
        if (operation && operation.op === 'qt_moc_invoke') {
          if (!call || !call.qtMoc || !call.invocation || operation !== activeOperation ||
              binding.argument !== 0 || !recipe.consumer || recipe.consumer.kind !== 'qt_moc' ||
              recipe.consumer.argument !== 0 || operation.receiver.native_path !== source ||
              !dataEqual(recipe.consumer.selection, operation.selection))
            fail('borrowed Qt receiver must bind its sole exact zero-argument void terminal operation');
          qtMocConsumerContract(recipe.consumer);
          return;
        }
        if (!call || !call.invocation || operation !== activeOperation || operation.op !== 'native_invoke' ||
            binding.argument !== 0 || !recipe.consumer || recipe.consumer.role !== 'cxx_this' ||
            recipe.consumer.argument !== 0 || operation.alias !== undefined ||
            !(operation.return_type === 'void' || borrowedRootScalars.has(operation.return_type)) ||
            operation.arguments.slice(1).some(argument => !borrowedRootScalarValue(argument)))
          fail('borrowed root dispatch must be a terminal scalar-only this call without aliases or nesting');
      } else {
        const operation = binding.operation, allowed = borrowedRootScalarFields(recipe);
        if (recipe.consumer !== undefined || operation !== activeOperation || !operation || operation.op !== 'native_read' ||
            !dataEqual(Object.keys(operation).sort(), ['bytes', 'fields', 'layout_evidence', 'op', 'receiver']) ||
            !operation.receiver || !dataEqual(Object.keys(operation.receiver), ['native_path']) ||
            operation.receiver.native_path !== source || operation.bytes !== layout.bytes ||
            !Array.isArray(operation.fields) || operation.fields.length < 1 || operation.fields.length > allowed.length)
          fail('borrowed root investigation must be one immediate scalar-only native_read');
        let previous = -1;
        for (const field of operation.fields) {
          const index = allowed.findIndex(candidate => dataEqual(candidate, field));
          if (index <= previous) fail('borrowed root read must preserve an admitted scalar projection subset/order');
          previous = index;
        }
      }
    }
    function receiverClosed(value, keys) {
      return value !== null && typeof value === 'object' && !Array.isArray(value) &&
        dataEqual(Object.keys(value).sort(), keys.slice().sort());
    }
    function receiverClaim(claim, file, sha256, pointer) {
      if (!receiverClosed(claim, ['kind', 'file', 'sha256', 'pointer']) ||
          claim.kind !== RECEIVER_DECLARATION_KIND || claim.file !== file || claim.sha256 !== sha256 || claim.pointer !== pointer)
        fail('receiver hypothesis needs its exact closed declaration claim');
    }
    function receiverFile(file) {
      return typeof file === 'string' && file.length <= 4096 && file.startsWith('/') && !file.includes('\0') &&
        file.slice(1).split('/').every(part => part.length > 0 && part !== '.' && part !== '..');
    }
    function receiverImageClaim(claim) {
      return receiverClosed(claim, ['path', 'sha256']) && receiverFile(claim.path) &&
        typeof claim.sha256 === 'string' && /^[a-f0-9]{64}$/.test(claim.sha256);
    }
    function receiverEntryClaim(entry) {
      if (!entry || !receiverImageClaim(entry.module)) return false;
      return receiverClosed(entry, ['module', 'offset']) && typeof entry.offset === 'string' &&
        /^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(entry.offset) &&
        Number.isSafeInteger(Number(entry.offset)) && Number(entry.offset) <= Number.MAX_SAFE_INTEGER - 4 && Number(entry.offset) % 4 === 0;
    }
    function assertReceiverHypothesisTerminal(recipe, binding, source) {
      const layout = recipe.types[0], getter = recipe.root.getter, consumer = recipe.consumer;
      const call = binding.call === undefined ? null : nativePathCalls.get(binding.call), operation = call && call.specification;
      if (!receiverClosed(recipe, ['format', 'root', 'root_type', 'result_type', 'types', 'path',
          'preconditions', 'root_evidence', 'lifetime', 'consumer']) ||
          !receiverClosed(recipe.root, ['getter']) ||
          !receiverClosed(getter, ['kind', 'target', 'abi', 'arguments', 'return_type', 'signature_evidence']) ||
          getter.kind !== 'native_function' || getter.abi !== 'default' || getter.return_type !== 'pointer' ||
          !receiverEntryClaim(getter.target) || !dataEqual(getter.arguments, []) ||
          recipe.root_type !== 'DeclaredReceiver' || recipe.result_type !== 'DeclaredReceiver' ||
          !dataEqual(recipe.path, []) || !dataEqual(recipe.preconditions, []) || recipe.types.length !== 1 ||
          !receiverClosed(layout, ['format', 'name', 'owner_image', 'abi', 'bytes', 'alignment', 'fields', 'runtime_identity', 'evidence']) ||
          layout.name !== 'DeclaredReceiver' || layout.bytes !== 8 || layout.alignment !== 8 ||
          !dataEqual(layout.fields, [{name: 'vptr', type: 'pointer', offset: 0}]) ||
          !receiverClosed(layout.runtime_identity, ['kind', 'field', 'image', 'data_offset']) ||
          layout.runtime_identity.kind !== 'pinned_data_vptr' || layout.runtime_identity.field !== 'vptr' ||
          !receiverImageClaim(layout.owner_image) || !dataEqual(layout.owner_image, layout.runtime_identity.image) ||
          !receiverClosed(recipe.lifetime, ['mode', 'evidence']) || recipe.lifetime.mode !== RECEIVER_HYPOTHESIS_MODE ||
          !receiverClosed(consumer, ['call', 'argument', 'pointee_type', 'role', 'dispatch', 'this_adjustment', 'evidence']) ||
          consumer.argument !== 0 || consumer.pointee_type !== 'DeclaredReceiver' || consumer.role !== 'cxx_this' ||
          consumer.dispatch !== 'direct' || consumer.this_adjustment !== 0 ||
          !consumer.call || !receiverClosed(consumer.call, ['module', 'offset',
            'abi', 'argument_types', 'return_type', 'context', 'pointer_policy', 'signature_evidence']) ||
          !receiverEntryClaim(Object.fromEntries(Object.entries(consumer.call)
            .filter(([key]) => ['module', 'symbol', 'offset'].includes(key)))) ||
          !call || call.qtMoc || !call.invocation || operation !== activeOperation || binding.argument !== 0 ||
          request.operations.length !== 1 || activeOperationIndex !== 0 || borrowedRootOccurrences(request) !== 1 ||
          !receiverClosed(operation, ['op', 'target', 'abi', 'arguments', 'return_type', 'signature_evidence']) ||
          operation.op !== 'native_invoke' || operation.abi !== 'default' || !receiverEntryClaim(operation.target) ||
          !Array.isArray(operation.arguments) ||
          operation.arguments.length < 1 || operation.arguments.length > 8 || dataEqual(getter.target, operation.target) ||
          !receiverClosed(operation.arguments[0], ['type', 'value']) || operation.arguments[0].type !== 'pointer' ||
          !receiverClosed(operation.arguments[0].value, ['native_path']) || operation.arguments[0].value.native_path !== source ||
          !(operation.return_type === 'void' || borrowedRootScalars.has(operation.return_type)) ||
          operation.arguments.slice(1).some(argument => !receiverClosed(argument, ['type', 'value']) || !borrowedRootScalarValue(argument)) ||
          result.native_calls !== 0 || result.invocations !== 0 || targetCallEpoch !== 0)
        fail('receiver hypothesis requires one closed immediate scalar-only primary-this dispatch');
      const claim = recipe.root_evidence && recipe.root_evidence.claim;
      if (!claim || !receiverFile(claim.file) ||
          typeof claim.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(claim.sha256))
        fail('receiver hypothesis needs one pinned model declaration');
      for (const [wrapper, pointer] of [[recipe.root_evidence, '/contracts/0'],
          [recipe.lifetime.evidence, '/contracts/1'], [layout.evidence, '/contracts/2'], [consumer.evidence, '/contracts/3']]) {
        if (!receiverClosed(wrapper, ['claim', 'originals'])) fail('receiver hypothesis evidence wrapper must be closed');
        receiverClaim(wrapper.claim, claim.file, claim.sha256, pointer);
        if (!dataEqual(wrapper.originals, recipe.root_evidence.originals))
          fail('receiver hypothesis evidence must bind the same exact binary observations');
      }
      receiverClaim(getter.signature_evidence, claim.file, claim.sha256, '/signatures/getter');
      receiverClaim(consumer.call.signature_evidence, claim.file, claim.sha256, '/signatures/consumer');
      receiverClaim(operation.signature_evidence, claim.file, claim.sha256, '/signatures/consumer');
    }
    function receiverClaimV2(claim, file, sha256, pointer) {
      if (!receiverClosed(claim, ['kind', 'file', 'sha256', 'pointer']) ||
          claim.kind !== RECEIVER_DECLARATION_V2_KIND || claim.file !== file || claim.sha256 !== sha256 || claim.pointer !== pointer)
        fail('V2 receiver hypothesis needs its exact closed declaration claim');
    }
    function validV2TailArgument(argument) {
      if (!receiverClosed(argument, ['type', 'value'])) return false;
      const type = argument.type, val = argument.value;
      if (typeof type === 'string') {
        if (type === 'utf8') return typeof val === 'string' && !val.includes('\0') && val.length <= 4096;
        return borrowedRootScalarValue(argument);
      }
      if (!type || typeof type !== 'object' || Array.isArray(type)) return false;
      if (type.kind === 'buffer') {
        if (!receiverClosed(type, ['kind', 'bytes', 'direction']) || !Number.isSafeInteger(type.bytes) ||
            type.bytes < 1 || type.bytes > 16384 || !['in', 'out', 'inout'].includes(type.direction))
          return false;
        if (type.direction === 'out') return val === null;
        return Array.isArray(val) && val.length === type.bytes &&
          val.every(b => Number.isSafeInteger(b) && b >= 0 && b <= 255);
      }
      if (type.kind === 'struct') {
        if (!receiverClosed(type, ['kind', 'fields']) || !Array.isArray(type.fields) ||
            type.fields.length < 1 || type.fields.length > 16 || val === null || typeof val !== 'object' || Array.isArray(val))
          return false;
        const fieldNames = type.fields.map(f => f && f.name);
        if (new Set(fieldNames).size !== type.fields.length || !receiverClosed(val, fieldNames)) return false;
        return type.fields.every(f => receiverClosed(f, ['name', 'type']) && typeof f.name === 'string' &&
          f.name.length >= 1 && f.name.length <= 64 && borrowedRootScalarValue({type: f.type, value: val[f.name]}));
      }
      return false;
    }
    function assertReceiverHypothesisV2Terminal(recipe, binding, source) {
      const getter = recipe.root && recipe.root.getter, consumer = recipe.consumer, vptr = recipe.vptr;
      const call = binding.call === undefined ? null : nativePathCalls.get(binding.call), operation = call && call.specification;
      if (!receiverClosed(recipe, ['format', 'root', 'hops', 'vptr', 'lifetime', 'consumer', 'originals', 'evidence_basis']) ||
          recipe.format !== RECEIVER_HYPOTHESIS_V2_FORMAT || !dataEqual(recipe.evidence_basis, RECEIVER_HYPOTHESIS_V2_BASIS) ||
          !receiverClosed(recipe.root, ['getter']) ||
          !receiverClosed(getter, ['kind', 'target', 'abi', 'arguments', 'return_type', 'signature_evidence']) ||
          getter.kind !== 'native_function' || getter.abi !== 'default' || getter.return_type !== 'pointer' ||
          !receiverEntryClaim(getter.target) || !dataEqual(getter.arguments, []) ||
          !Array.isArray(recipe.hops) || recipe.hops.length > 3 ||
          !vptr || !['image', 'data_offset'].every(k => Object.prototype.hasOwnProperty.call(vptr, k)) ||
          Object.keys(vptr).some(k => !['image', 'data_offset', 'subobject_offset', 'pac_mode'].includes(k)) ||
          !receiverImageClaim(vptr.image) || typeof vptr.data_offset !== 'string' ||
          !/^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(vptr.data_offset) ||
          !Number.isSafeInteger(Number(vptr.data_offset)) || Number(vptr.data_offset) % 8 !== 0 ||
          !Number.isSafeInteger(vptr.subobject_offset ?? 0) || (vptr.subobject_offset ?? 0) < 0 ||
          (vptr.subobject_offset ?? 0) > 65536 || (vptr.subobject_offset ?? 0) % 8 !== 0 ||
          !['none', 'arm64e_strip'].includes(vptr.pac_mode || 'none'))
        fail('V2 receiver hypothesis requires its closed bounded root, hop, and vptr contract');
      const claim = getter.signature_evidence;
      if (!claim || !receiverFile(claim.file) || typeof claim.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(claim.sha256))
        fail('V2 receiver hypothesis needs one pinned model declaration');
      receiverClaimV2(claim, claim.file, claim.sha256, '/signatures/getter');
      let stepGetters = 0;
      for (let idx = 0; idx < recipe.hops.length; idx++) {
        const hop = recipe.hops[idx];
        if (!hop || typeof hop !== 'object' || Array.isArray(hop) || typeof hop.name !== 'string' ||
            hop.name.length < 1 || hop.name.length > 64)
          fail('V2 receiver hop must be a named closed object');
        if (hop.kind === 'field_pointer') {
          if (!receiverClosed(hop, ['kind', 'name', 'field_offset', 'non_null']) ||
              !Number.isSafeInteger(hop.field_offset) || hop.field_offset < 0 || hop.field_offset > 65536 ||
              hop.field_offset % 8 !== 0 || hop.non_null !== true)
            fail('V2 field_pointer hop is invalid');
        } else if (hop.kind === 'array_element') {
          if (!['kind', 'name', 'base_offset', 'index', 'stride', 'non_null'].every(k => Object.prototype.hasOwnProperty.call(hop, k)) ||
              Object.keys(hop).some(k => !['kind', 'name', 'base_offset', 'index', 'stride', 'non_null', 'element_kind'].includes(k)) ||
              (hop.element_kind || 'pointer') !== 'pointer' || !Number.isSafeInteger(hop.base_offset) ||
              hop.base_offset < 0 || hop.base_offset > 65536 || hop.base_offset % 8 !== 0 ||
              !Number.isSafeInteger(hop.index) || hop.index < 0 || hop.index > 1024 ||
              !Number.isSafeInteger(hop.stride) || hop.stride < 8 || hop.stride > 4096 || hop.stride % 8 !== 0 ||
              hop.non_null !== true)
            fail('V2 array_element hop is invalid');
        } else if (hop.kind === 'vector_element') {
          if (!['kind', 'name', 'vector_offset', 'index', 'non_null'].every(k => Object.prototype.hasOwnProperty.call(hop, k)) ||
              Object.keys(hop).some(k => !['kind', 'name', 'vector_offset', 'index', 'non_null', 'element_kind'].includes(k)) ||
              (hop.element_kind || 'pointer') !== 'pointer' || !Number.isSafeInteger(hop.vector_offset) ||
              hop.vector_offset < 0 || hop.vector_offset > 65536 || hop.vector_offset % 8 !== 0 ||
              !Number.isSafeInteger(hop.index) || hop.index < 0 || hop.index > 1024 || hop.non_null !== true)
            fail('V2 vector_element hop is invalid');
        } else if (hop.kind === 'step_getter') {
          stepGetters++;
          if (!receiverClosed(hop, ['kind', 'name', 'target', 'abi', 'signature_evidence']) ||
              hop.abi !== 'default' || !receiverEntryClaim(hop.target) ||
              dataEqual(hop.target, getter.target) || dataEqual(hop.target, consumer && consumer.entry))
            fail('V2 step_getter hop is invalid or aliases root getter/consumer');
          receiverClaimV2(hop.signature_evidence, claim.file, claim.sha256, '/signatures/hop_' + idx);
        } else {
          fail('unsupported V2 hop kind');
        }
      }
      const expectedCalls = 2 + stepGetters;
      if (!receiverClosed(recipe.lifetime, ['mode', 'queue', 'scope', 'expected_native_calls']) ||
          recipe.lifetime.mode !== RECEIVER_HYPOTHESIS_V2_MODE || recipe.lifetime.queue !== 'main' ||
          recipe.lifetime.scope !== 'single_immediate_transaction' ||
          recipe.lifetime.expected_native_calls !== expectedCalls ||
          !consumer || !['dispatch', 'vtable_slot_offset', 'this_adjustment', 'entry', 'abi', 'argument_types', 'return_type', 'signature_evidence']
            .every(k => Object.prototype.hasOwnProperty.call(consumer, k)) ||
          Object.keys(consumer).some(k => !['dispatch', 'vtable_slot_offset', 'this_adjustment', 'pac_mode', 'entry', 'abi', 'argument_types', 'return_type', 'signature_evidence'].includes(k)) ||
          !['direct', 'vtable_slot'].includes(consumer.dispatch) ||
          (consumer.dispatch === 'direct' ? consumer.vtable_slot_offset !== null :
            !Number.isSafeInteger(consumer.vtable_slot_offset) || consumer.vtable_slot_offset < 0 ||
            consumer.vtable_slot_offset > 1024 || consumer.vtable_slot_offset % 8 !== 0) ||
          !Number.isSafeInteger(consumer.this_adjustment) || consumer.this_adjustment < -4096 ||
          consumer.this_adjustment > 4096 || Math.abs(consumer.this_adjustment) % 8 !== 0 ||
          !['none', 'arm64e_strip'].includes(consumer.pac_mode || 'none') ||
          !receiverEntryClaim(consumer.entry) || dataEqual(getter.target, consumer.entry) ||
          consumer.abi !== 'default' || !Array.isArray(consumer.argument_types) ||
          consumer.argument_types.length < 1 || consumer.argument_types.length > 8 ||
          consumer.argument_types[0] !== 'pointer' ||
          !(consumer.return_type === 'void' || consumer.return_type === 'utf8' || borrowedRootScalars.has(consumer.return_type)) ||
          !call || call.qtMoc || !call.invocation || operation !== activeOperation || binding.argument !== 0 ||
          request.operations.length !== 1 || activeOperationIndex !== 0 || borrowedRootOccurrences(request) !== 1 ||
          !operation || !['op', 'target', 'abi', 'arguments', 'return_type', 'signature_evidence']
            .every(k => Object.prototype.hasOwnProperty.call(operation, k)) ||
          Object.keys(operation).some(k => !['op', 'dispatch', 'vtable_slot_offset', 'target', 'abi', 'arguments', 'return_type', 'signature_evidence'].includes(k)) ||
          operation.op !== 'native_invoke' || operation.abi !== 'default' || !receiverEntryClaim(operation.target) ||
          !Array.isArray(operation.arguments) || operation.arguments.length !== consumer.argument_types.length ||
          !receiverClosed(operation.arguments[0], ['type', 'value']) || operation.arguments[0].type !== 'pointer' ||
          !receiverClosed(operation.arguments[0].value, ['native_path']) || operation.arguments[0].value.native_path !== source ||
          operation.arguments.slice(1).some(arg => !validV2TailArgument(arg)) ||
          result.native_calls !== 0 || result.invocations !== 0 || targetCallEpoch !== 0)
        fail('V2 receiver hypothesis requires one closed immediate terminal C++ dispatch');
      receiverClaimV2(consumer.signature_evidence, claim.file, claim.sha256, '/signatures/consumer');
      receiverClaimV2(operation.signature_evidence, claim.file, claim.sha256, '/signatures/consumer');
    }
    function assertBorrowedRootCurrent(prepared) {
      if (prepared.recipe.lifetime.mode === 'main_queue_observed_root') return assertObservedCurrent(prepared);
      if (!['main_queue_borrowed_root', RECEIVER_HYPOTHESIS_MODE, RECEIVER_HYPOTHESIS_V2_MODE].includes(prepared.recipe.lifetime.mode)) return null;
      assertNativePathCurrent(v2AllowsArm64e(prepared.recipe));
      const scope = borrowedRootScopes.get(prepared.borrowedScope);
      if (!scope || scope.prepared !== prepared || scope.transaction !== nativePathTransaction || scope.phase !== 'ready' ||
          scope.getterEpoch === null || scope.getterEpoch !== targetCallEpoch ||
          activeOperation !== scope.operation || activeOperationIndex !== scope.index ||
          !dataEqual(activeOperation, scope.operationSnapshot))
        fail('borrowed root epoch/operation expired or changed; no reacquisition');
      if (receiverHypothesis(prepared.recipe) && (scope.getterEpoch !== 1 || result.native_calls !== 1 || result.invocations !== 0))
        fail('receiver hypothesis cannot admit intervening or nested target calls');
      if (receiverHypothesisV2(prepared.recipe)) {
        const expectedEpoch = scope.expectedEpoch ?? 1;
        if (scope.getterEpoch !== expectedEpoch || result.native_calls !== expectedEpoch || result.invocations !== 0)
          fail('V2 receiver hypothesis cannot admit intervening or uncounted target calls');
      }
      return scope;
    }
    function consumeBorrowedRoot(prepared, use) {
      const scope = assertBorrowedRootCurrent(prepared);
      if (!scope || scope.use !== use || !scope.observation) fail('borrowed root single use is not bound to this operation');
      scope.phase = 'consumed';
      Object.assign(scope.observation.borrowed_scope, {checked_epoch: targetCallEpoch, consumed: true});
    }
    function prepareReceiverHypothesisV2(source, recipe, binding) {
      if (binding.call === undefined) fail('V2 native receiver requires an actual terminal call binding');
      const call = nativePathCalls.get(binding.call);
      if (!call) fail('V2 native receiver actual call binding is unknown');
      assertPathConsumer(recipe, call.specification, binding.argument);
      assertReceiverHypothesisV2Terminal(recipe, binding, source);
      const resources = new Map(), images = new Map(), originals = new Map();
      function pin(file, sha) {
        const value = (payload.native_resources || {})[file];
        if (!value || value.file !== file || value.sha256 !== sha) fail('native path resource is not independently pinned');
        resources.set(file, pathFreeze(pathCopy(value)));
        return value;
      }
      function image(claim) {
        pin(claim.path, claim.sha256);
        const prior = images.get(claim.path);
        if (prior && !dataEqual(prior, claim)) fail('native path image claims conflict');
        images.set(claim.path, claim);
      }
      image(recipe.root.getter.target.module);
      image(recipe.consumer.entry.module);
      image(recipe.vptr.image);
      for (const hop of recipe.hops) if (hop.kind === 'step_getter') image(hop.target.module);
      const claim = recipe.root.getter.signature_evidence;
      const declaration = pin(claim.file, claim.sha256);
      const expectedRoles = ['getter_code', 'consumer_code', 'vptr_data'];
      if (recipe.consumer.dispatch === 'vtable_slot') expectedRoles.push('vtable_slot_data');
      if (!Array.isArray(recipe.originals) || recipe.originals.length !== expectedRoles.length ||
          new Set(recipe.originals.map(row => row && row.file)).size !== expectedRoles.length ||
          recipe.originals.some((row, index) => !receiverClosed(row, ['role', 'file', 'sha256']) ||
            row.role !== expectedRoles[index] || !receiverFile(row.file) || row.file === claim.file ||
            typeof row.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.sha256)))
        fail('V2 receiver hypothesis requires its ordered distinct binary observation closure');
      for (const row of recipe.originals) {
        const original = pin(row.file, row.sha256);
        if (!Number.isSafeInteger(original.bytes) || original.bytes < 1) fail('native path original evidence size is invalid');
        originals.set(row.file, original);
      }
      const expectedContracts = [
        ['/contracts/0', pathRootContract(recipe)],
        ['/contracts/1', pathLifetimeContract(recipe)],
        ['/contracts/2', pathVptrContractV2(recipe.vptr)],
        ['/contracts/3', pathConsumerContractV2(recipe.consumer)]
      ];
      if (!Array.isArray(declaration.native_path_contracts) || declaration.native_path_contracts.length !== 4 ||
          declaration.native_path_contracts.some(row => !receiverClosed(row, ['pointer', 'contract', 'originals'])) ||
          new Set(declaration.native_path_contracts.map(row => row.pointer)).size !== 4)
        fail('V2 receiver hypothesis requires its closed four-contract binary observation closure');
      for (const [pointer, contract] of expectedContracts) {
        const matches = declaration.native_path_contracts.filter(row => row.pointer === pointer &&
          dataEqual(row.contract, contract) && dataEqual(row.originals, recipe.originals));
        if (matches.length !== 1) fail('V2 native receiver selected contract/original correlation was not admitted');
      }
      const signature = (argument_types, return_type) => ({abi: 'default', argument_types, return_type,
        context: false, pointer_policy: 'borrowed_no_escape'});
      const wantedSignatures = [{pointer: '/signatures/getter', signature: signature([], 'pointer')}];
      recipe.hops.forEach((hop, idx) => {
        if (hop.kind === 'step_getter')
          wantedSignatures.push({pointer: '/signatures/hop_' + idx, signature: signature(['pointer'], 'pointer')});
      });
      wantedSignatures.push({pointer: '/signatures/consumer',
        signature: signature(recipe.consumer.argument_types, recipe.consumer.return_type)});
      const selectedSignatures = declaration.native_receiver_signatures;
      if (!Array.isArray(selectedSignatures) || selectedSignatures.length !== wantedSignatures.length ||
          selectedSignatures.some(row => !receiverClosed(row, ['pointer', 'signature'])) ||
          new Set(selectedSignatures.map(row => row.pointer)).size !== wantedSignatures.length ||
          !wantedSignatures.every(row => selectedSignatures.some(candidate => dataEqual(candidate, row))))
        fail('V2 receiver hypothesis model signatures differ from its declared getter/hop/consumer signatures');
      if (originals.size > 64 || Array.from(originals.values()).reduce((sum, row) => sum + row.bytes, 0) > 128 * 1024 * 1024)
        fail('native path original evidence closure exceeds its fixed budget');
      const prepared = {source, recipe, resources, images, reached: [], binding};
      assertNativePathResources(prepared);
      return prepared;
    }
    function prepareNativePath(source, binding) {
      const recipe = pathFreeze(pathCopy(source));
      assertNativePathCurrent(v2AllowsArm64e(recipe));
      if (receiverHypothesisV2(recipe)) return prepareReceiverHypothesisV2(source, recipe, binding);
      const hypothesis = receiverHypothesis(recipe);
      if (!['anyuse.rea-native-pointer-path/1', RECEIVER_HYPOTHESIS_FORMAT].includes(recipe.format) || !Array.isArray(recipe.path) ||
          recipe.path.length < (recipe.lifetime &&
            ['main_queue_borrowed_root', 'main_queue_observed_root', RECEIVER_HYPOTHESIS_MODE].includes(recipe.lifetime.mode) ? 0 : 1) ||
          recipe.path.length > 6 || !Array.isArray(recipe.types) ||
          recipe.types.length < 1 || recipe.types.length > 7 || !Array.isArray(recipe.preconditions) || recipe.preconditions.length > 24 ||
          !recipe.root || Object.keys(recipe.root).length !== 1 ||
          !['objc', 'read', 'swift_read', 'getter', 'data', 'lifecycle'].includes(Object.keys(recipe.root)[0]) ||
          !recipe.lifetime || !['process_lifetime', 'retained_owner_main_queue_stable', 'main_queue_borrowed_root',
            'main_queue_observed_root', RECEIVER_HYPOTHESIS_MODE].includes(recipe.lifetime.mode) ||
          hypothesis !== (recipe.lifetime.mode === RECEIVER_HYPOTHESIS_MODE))
        fail('native path requires its complete bounded typed lifetime contract');
      if ((recipe.root.lifecycle !== undefined) !== (recipe.lifetime.mode === 'main_queue_observed_root'))
        fail('lifecycle root and observed lifetime must be declared together');
      if (recipe.root.getter !== undefined && (recipe.root.getter.abi !== 'default' ||
          recipe.root.getter.return_type !== 'pointer' || recipe.root.getter.arguments.length !== 0 ||
          recipe.root.getter.context !== undefined || recipe.root.getter.compiler_adapter !== undefined ||
          !['process_lifetime', 'main_queue_borrowed_root', RECEIVER_HYPOTHESIS_MODE].includes(recipe.lifetime.mode)))
        fail('native path getter root has no supported borrowed lifetime');
      if (recipe.root.objc !== undefined && recipe.root.objc.kind !== 'unique') fail('native path root requires one exact live owner');
      if (recipe.root.data !== undefined) {
        const data = recipe.root.data;
        if (!data || typeof data !== 'object' || Array.isArray(data) ||
            !dataEqual(Object.keys(data).sort(), ['mode', 'module', 'offset']) ||
            !data.module || typeof data.module !== 'object' || Array.isArray(data.module) ||
            !dataEqual(Object.keys(data.module).sort(), ['path', 'sha256']) ||
            typeof data.module.path !== 'string' || !data.module.path.startsWith('/') ||
            typeof data.module.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(data.module.sha256) ||
            typeof data.offset !== 'string' || !/^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(data.offset) ||
            !Number.isSafeInteger(Number(data.offset)) || !['object', 'pointer'].includes(data.mode) ||
            recipe.lifetime.mode !== 'process_lifetime')
          fail('native path data root requires one closed image-relative process-lifetime location');
      }
      if (binding.call !== undefined) {
        const call = nativePathCalls.get(binding.call);
        if (!call) fail('native path actual call binding is unknown');
        assertPathConsumer(recipe, call.specification, binding.argument);
      } else if (binding.investigation !== true) fail('native path needs an explicit investigation or actual call');
      if (hypothesis) assertReceiverHypothesisTerminal(recipe, binding, source);
      const resources = new Map(), images = new Map(), originals = new Map(), layouts = new Map();
      function pin(file, sha) {
        const value = (payload.native_resources || {})[file];
        if (!value || value.file !== file || value.sha256 !== sha) fail('native path resource is not independently pinned');
        resources.set(file, pathFreeze(pathCopy(value)));
        return value;
      }
      function image(claim) {
        pin(claim.path, claim.sha256);
        const prior = images.get(claim.path);
        if (prior && !dataEqual(prior, claim)) fail('native path image claims conflict');
        images.set(claim.path, claim);
      }
      function evidence(wrapper, contract) {
        if (!wrapper || !wrapper.claim || !Array.isArray(wrapper.originals) || wrapper.originals.length < 1 || wrapper.originals.length > 16)
          fail('native path needs independently attributed original evidence');
        const claim = wrapper.claim, value = pin(claim.file, claim.sha256);
        const matches = (value.native_path_contracts || []).filter(row => row.pointer === claim.pointer &&
          dataEqual(row.contract, contract) && dataEqual(row.originals, wrapper.originals));
        if (matches.length !== 1) fail('native path selected contract/original correlation was not admitted');
        if (hypothesis) {
          const roles = ['getter_code', 'consumer_code', 'vptr_data'];
          if (wrapper.originals.length !== roles.length || new Set(wrapper.originals.map(row => row.file)).size !== roles.length ||
              wrapper.originals.some((row, index) =>
              !receiverClosed(row, ['role', 'file', 'sha256']) || row.role !== roles[index] ||
              !receiverFile(row.file) ||
              typeof row.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.sha256)) ||
              !Array.isArray(value.native_path_contracts) || value.native_path_contracts.length !== 4 ||
              value.native_path_contracts.some(row => !receiverClosed(row, ['pointer', 'contract', 'originals'])) ||
              new Set(value.native_path_contracts.map(row => row.pointer)).size !== 4)
            fail('receiver hypothesis requires its closed four-contract binary observation closure');
        } else if (!wrapper.originals.some(row => ['header', 'dwarf', 'source', 'original_ir', 'independent_review'].includes(row.role)))
          fail('native path needs original type/lifetime evidence, not a build label');
        for (const row of wrapper.originals) {
          if (row.file === claim.file) fail('native path evidence cannot be a self-echo');
          const original = pin(row.file, row.sha256);
          if (!Number.isSafeInteger(original.bytes) || original.bytes < 1) fail('native path original evidence size is invalid');
          const prior = originals.get(row.file);
          if (prior && prior.sha256 !== row.sha256) fail('native path original evidence conflicts');
          originals.set(row.file, original);
        }
      }
      const widths = {bool: 1, int8: 1, uint8: 1, int16: 2, uint16: 2, int32: 4, uint32: 4,
        int64: 8, uint64: 8, long: 8, ulong: 8, float: 4, double: 8, pointer: 8};
      for (const layout of recipe.types) {
        if (layout.format !== 'anyuse.rea-native-pointer-layout/2' || layouts.has(layout.name) ||
            !dataEqual(layout.abi, {architecture: 'arm64', cpu_subtype: 0, pointer_bytes: 8, byte_order: 'little'}) ||
            !Number.isSafeInteger(layout.bytes) || layout.bytes < 1 || layout.bytes > 16384 ||
            ![1, 2, 4, 8, 16].includes(layout.alignment) || layout.bytes % layout.alignment !== 0 ||
            !Array.isArray(layout.fields) || layout.fields.length < 1 || layout.fields.length > 32)
          fail('native path layout is not a bounded natural plain-arm64 declaration');
        const names = new Set(), occupied = [];
        for (const field of layout.fields) {
          const width = widths[field.type];
          if (!width || names.has(field.name) || !Number.isSafeInteger(field.offset) || field.offset < 0 ||
              field.offset + width > layout.bytes || field.offset % Math.min(width, 8) !== 0 ||
              layout.alignment < Math.min(width, 8) || occupied.some(range => field.offset < range[1] && field.offset + width > range[0]))
            fail('native path field is ambiguous, overlapping, unaligned or outside its declared object');
          names.add(field.name); occupied.push([field.offset, field.offset + width]);
          if (field.target !== undefined && field.type !== 'pointer') fail('native path edge must be an ordinary pointer');
        }
        layouts.set(layout.name, layout); image(layout.owner_image);
        evidence(layout.evidence, hypothesis ? receiverLayoutContract(layout) : pathLayoutContract(layout));
        if (layout.runtime_identity !== undefined) {
          const identity = layout.runtime_identity, field = pathField(layout, identity.field);
          if (identity.kind !== 'pinned_data_vptr' || field.type !== 'pointer' || field.target !== undefined ||
              typeof identity.data_offset !== 'string' || !/^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(identity.data_offset))
            fail('native path identity must be pinned data, never virtual dispatch');
          image(identity.image);
        }
      }
      if (recipe.types.reduce((sum, layout) => sum + layout.bytes, 0) > 65536) fail('native path declared extent budget exceeded');
      const reached = [], seenTypes = new Set();
      let type = recipe.root_type, visitedBytes = 0;
      for (let index = 0; index <= recipe.path.length; index++) {
        const layout = layouts.get(type);
        if (!layout) fail('native path declared hop type is missing');
        reached.push(layout); seenTypes.add(type); visitedBytes += layout.bytes;
        if (index < recipe.path.length) {
          const field = pathField(layout, recipe.path[index]);
          if (field.type !== 'pointer' || field.target === undefined) fail('native path hop is not a typed pointer edge');
          type = field.target;
        }
      }
      if (type !== recipe.result_type || seenTypes.size !== layouts.size || visitedBytes > 65536)
        fail('native path result/visited-instance declarations differ or exceed budget');
      const guards = new Set();
      for (const guard of recipe.preconditions) {
        if (!Number.isSafeInteger(guard.hop) || guard.hop < 0 || guard.hop >= reached.length) fail('native path guard hop is invalid');
        const field = pathField(reached[guard.hop], guard.field), key = guard.hop + '/' + guard.field;
        if (guards.has(key) || ['pointer', 'float', 'double'].includes(field.type)) fail('native path guard needs one exact integer/Bool field');
        guards.add(key);
      }
      if (recipe.lifetime.mode === 'main_queue_borrowed_root') assertBorrowedRootTerminal(recipe, binding, source);
      if (recipe.lifetime.mode === 'main_queue_observed_root') assertObservedTerminal(recipe, binding, source);
      evidence(recipe.root_evidence, pathRootContract(recipe));
      evidence(recipe.lifetime.evidence, pathLifetimeContract(recipe));
      if (recipe.root.lifecycle !== undefined) {
        const spec = recipe.root.lifecycle;
        evidence(spec.evidence, lifecycleObserverContract(recipe));
        for (const entry of [spec.constructor.entry, ...spec.constructor.return_sites, ...spec.destructors.map(row => row.entry)])
          image(entry.module);
        lifecycleSpecification(recipe);
      }
      if (recipe.consumer !== undefined) {
        evidence(recipe.consumer.evidence, pathConsumerContract(recipe.consumer, hypothesis));
        if (recipe.consumer.kind === 'qt_moc') {
          const selection = recipe.consumer.selection;
          image(selection.module); image(selection.qt_core);
          for (const key of ['catalog', 'code', 'table', 'qt_sources']) pin(selection[key].file, selection[key].sha256);
        } else {
          image(recipe.consumer.call.module);
          pin(recipe.consumer.call.signature_evidence.file, recipe.consumer.call.signature_evidence.sha256);
        }
      }
      if (recipe.root.getter !== undefined) {
        image(recipe.root.getter.target.module); pin(recipe.root.getter.signature_evidence.file, recipe.root.getter.signature_evidence.sha256);
      }
      if (hypothesis) {
        const claim = recipe.root_evidence.claim, declaration = resources.get(claim.file);
        const signature = (argument_types, return_type) => ({abi: 'default', argument_types, return_type,
          context: false, pointer_policy: 'borrowed_no_escape'});
        const wanted = [{pointer: '/signatures/getter', signature: signature([], 'pointer')},
          {pointer: '/signatures/consumer', signature: signature(recipe.consumer.call.argument_types, recipe.consumer.call.return_type)}];
        const selected = declaration.native_receiver_signatures;
        if (!Array.isArray(selected) || selected.length !== wanted.length ||
            selected.some(row => !receiverClosed(row, ['pointer', 'signature'])) ||
            new Set(selected.map(row => row.pointer)).size !== wanted.length ||
            !wanted.every(row => selected.some(candidate => dataEqual(candidate, row))))
          fail('receiver hypothesis model signatures differ from its exact scalar getter/consumer declarations');
      }
      if (recipe.root.data !== undefined) image(recipe.root.data.module);
      for (const tag of ['read', 'swift_read']) if (recipe.root[tag] !== undefined) {
        image(recipe.root[tag].module); pin(recipe.root[tag].evidence.file, recipe.root[tag].evidence.sha256);
      }
      if (originals.size > 64 || Array.from(originals.values()).reduce((sum, row) => sum + row.bytes, 0) > 128 * 1024 * 1024)
        fail('native path original evidence closure exceeds its fixed budget');
      const prepared = {source, recipe, resources, images, reached, binding};
      assertNativePathResources(prepared);
      return prepared;
    }
    function assertNativePathResources(prepared) {
      const recipe = prepared.recipe;
      const allowArm64e = v2AllowsArm64e(recipe);
      assertNativePathCurrent(allowArm64e);
      if (!dataEqual(prepared.source, prepared.recipe)) fail('native path recipe changed; no reacquisition');
      for (const [file, wanted] of prepared.resources)
        if (!dataEqual((payload.native_resources || {})[file], wanted)) fail('native path pinned resource closure changed');
      for (const claim of prepared.images.values()) {
        imageCache.delete(claim.path);
        const image = moduleFor(claim);
        if (image.identity.cpu_type !== 0x0100000c || !validArm64Subtype(image.identity.cpu_subtype, allowArm64e))
          fail('native path refuses non-plain-arm64/PAC image declarations');
        if (receiverHypothesisV2(recipe) && (image.identity.cpu_subtype & 0xffffff) === 2) {
          if (recipe.vptr.pac_mode !== 'arm64e_strip' ||
              (recipe.consumer.dispatch === 'vtable_slot' && recipe.consumer.pac_mode !== 'arm64e_strip'))
            fail('arm64e V2 receiver image requires explicit pac_mode=arm64e_strip');
        }
      }
      if (receiverHypothesisV2(recipe)) {
        const vtableBytes = recipe.consumer.dispatch === 'vtable_slot' ? recipe.consumer.vtable_slot_offset + 8 : 8;
        nativePathDataLocation(recipe.vptr.image, recipe.vptr.data_offset, vtableBytes, 8, prepared);
        entryFor(recipe.root.getter.target, recipe.root.getter.signature_evidence, recipe.root.getter.abi);
        for (const hop of recipe.hops) if (hop.kind === 'step_getter')
          entryFor(hop.target, hop.signature_evidence, hop.abi);
        entryFor(recipe.consumer.entry, recipe.consumer.signature_evidence, recipe.consumer.abi);
        return;
      }
      if (recipe.root.lifecycle !== undefined) lifecycleSpecification(recipe);
      if (recipe.root.data !== undefined) nativePathDataRootLocation(prepared);
      for (const layout of recipe.types) if (layout.runtime_identity !== undefined)
        nativePathDataIdentity(layout.runtime_identity, prepared);
      if (recipe.root.getter !== undefined)
        entryFor(recipe.root.getter.target, recipe.root.getter.signature_evidence, recipe.root.getter.abi);
      if (recipe.consumer !== undefined) {
        if (recipe.consumer.kind === 'qt_moc') {
          const operation = prepared.binding.call === undefined ? null : nativePathCalls.get(prepared.binding.call);
          if (!operation || !operation.qtMoc) fail('Qt metadata lost its private terminal binding');
          assertQtMocRuntimeCurrent(operation.qtMoc.pin);
          entryFor({module: recipe.consumer.selection.module, offset: recipe.consumer.selection.dispatcher_offset},
            recipe.consumer.evidence.claim, 'default');
        } else {
          const call = recipe.consumer.call, target = {module: call.module};
          if (call.symbol !== undefined) target.symbol = call.symbol; else target.offset = call.offset;
          entryFor(target, call.signature_evidence, call.abi);
        }
      }
    }
    function nativePathDataLocation(claim, encodedOffset, bytes, alignment, prepared) {
      const image = moduleFor(claim), offset = Number(encodedOffset), end = offset + bytes;
      if (typeof encodedOffset !== 'string' || !/^0x(?:0|[1-9a-f][0-9a-f]{0,15})$/.test(encodedOffset) ||
          !Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(bytes) || bytes < 1 ||
          ![1, 2, 4, 8, 16].includes(alignment) || offset % alignment !== 0 ||
          !Number.isSafeInteger(end) || end > image.extent)
        fail('native path data location is unaligned or exceeds its pinned image');
      const intersects = section => Number(section.offset) < end && Number(section.offset) + section.bytes > offset;
      // Every intersecting loaded section counts, including code and partial
      // overlaps. A containing data section cannot hide another section.
      const sections = image.sections.filter(intersects), pin = prepared.resources.get(claim.path);
      const declared = pin && Array.isArray(pin.native_path_data_sections) &&
        pin.native_path_data_sections.filter(row => sameImage(row.image_identity, image.identity));
      const originals = declared && declared.length === 1 && Array.isArray(declared[0].sections) &&
        declared[0].sections.filter(intersects);
      if (sections.length !== 1 || !originals || originals.length !== 1 || !dataEqual(originals[0], sections[0]))
        fail('native path data location lacks one exact pinned file-backed non-instruction section');
      const section = sections[0], start = Number(section.offset), sectionEnd = start + section.bytes;
      if ((section.flags & 0x80000400) !== 0 || [1, 0x0c, 0x12].includes(section.flags & 0xff) ||
          offset < start || end > sectionEnd)
        fail('native path data location crosses a section boundary or is code/zero-fill');
      const segments = image.segments.filter(segment => segment.segment === section.segment &&
        start >= Number(segment.offset) && sectionEnd <= Number(segment.offset) + segment.bytes);
      if (segments.length !== 1) fail('native path data section has no exact loaded file segment');
      const segment = segments[0], relative = start - Number(segment.offset);
      const fileOffset = image.sectionFileOffsets.get(section);
      if (!Number.isSafeInteger(fileOffset) || relative + section.bytes > segment.file_bytes ||
          fileOffset < segment.file_offset || fileOffset - segment.file_offset !== relative ||
          fileOffset + section.bytes > segment.file_offset + segment.file_bytes)
        fail('native path data section exceeds or disagrees with its original file-backed segment');
      const address = image.module.base.add(encodedOffset);
      if (!address.and(ptr(alignment - 1)).isNull()) fail('native path loaded data address is misaligned');
      return {image, address: inImage(image, address, bytes)};
    }
    function nativePathDataRootLocation(prepared) {
      const data = prepared.recipe.root.data, layout = prepared.reached[0];
      return nativePathDataLocation(data.module, data.offset, data.mode === 'object' ? layout.bytes : 8,
        data.mode === 'object' ? layout.alignment : 8, prepared);
    }
    function nativePathDataIdentity(identity, prepared) {
      return nativePathDataLocation(identity.image, identity.data_offset, 8, 8, prepared).address;
    }
    function registerNativePathOwner(root, owner) {
      hold(owner);
      const key = owner.handle.toString(), entry = retained.get(key);
      if (!entry || entry.object.handle.compare(owner.handle) !== 0) fail('native path retained owner was not registered');
      root.owner = {key, entry};
    }
    function assertNativePathRoot(root, prepared) {
      assertBorrowedRootCurrent(prepared);
      if (root.owner !== null && retained.get(root.owner.key) !== root.owner.entry)
        fail('native path retained root owner expired');
      if (prepared.recipe.lifetime.mode === 'retained_owner_main_queue_stable' && root.owner === null)
        fail('native path has no actual retained owner for its lifetime premise');
      if (root.data !== undefined) {
        const current = nativePathDataRootLocation(prepared);
        if (root.owner !== null || prepared.recipe.lifetime.mode !== 'process_lifetime' ||
            current.image.module.base.compare(root.data.base) !== 0 || current.address.compare(root.data.address) !== 0)
          fail('native path data root image/location changed; no reacquisition');
      }
      for (const read of root.reads) {
        let actual;
        try { actual = read.type === 'pointer' ? readable(read.slot, 8).readPointer() : readable(read.slot, 1).readU8(); }
        catch (_error) { fail('native path root slot became unreadable'); }
        if (read.type === 'pointer' ? actual.compare(read.value) !== 0 : actual !== Number(read.value))
          fail('native path root slot/lifecycle guard changed; no reacquisition');
      }
    }
    function nativePathSnapshot(pointer, layout, hop, prepared) {
      assertNativePathCurrent();
      assertBorrowedRootCurrent(prepared);
      if (!pointer || pointer.isNull() || !pointer.and(ptr(layout.alignment - 1)).isNull())
        fail('native path pointer is null or misaligned');
      const values = new Map();
      if (layout.runtime_identity !== undefined) {
        // Check only the admitted identity slot before using the derived
        // extent or fields. This witness does not establish pointee lifetime.
        const identity = layout.runtime_identity, field = pathField(layout, identity.field);
        let value;
        assertBorrowedRootCurrent(prepared);
        try { value = memoryField(pointer, field); }
        catch (_error) { fail('native path declared memory read failed'); }
        if (value.compare(nativePathDataIdentity(identity, prepared)) !== 0)
          fail('native path pinned data identity differs from its exact non-instruction section');
        values.set(identity.field, value);
      }
      assertBorrowedRootCurrent(prepared);
      readable(pointer, layout.bytes);
      const names = new Set(prepared.recipe.preconditions.filter(guard => guard.hop === hop).map(guard => guard.field));
      if (hop < prepared.recipe.path.length) names.add(prepared.recipe.path[hop]);
      for (const name of Array.from(names).sort()) {
        assertBorrowedRootCurrent(prepared);
        if (values.has(name)) continue;
        const field = pathField(layout, name);
        let value;
        try { value = memoryField(pointer, field); }
        catch (_error) { fail('native path declared memory read failed'); }
        if (field.type === 'bool') {
          if (value !== 0 && value !== 1) fail('native path Bool guard has no canonical byte representation');
          value = value === 1;
        } else if (['int64', 'uint64', 'long', 'ulong'].includes(field.type)) value = value.toString();
        values.set(name, value);
      }
      for (const guard of prepared.recipe.preconditions.filter(item => item.hop === hop)) {
        const actual = values.get(guard.field);
        if (typeof actual !== typeof guard.equals || actual !== guard.equals) fail('native path literal guard refused');
      }
      return values;
    }
    function nativePathArgumentObject(call, argument) {
      if (call.qtMoc) return argument === 0 ? call.specification.receiver : null;
      return call.specification.arguments[argument];
    }
    function prepareNativePathCall(specification, invocation) {
      if ((specification.context && containsNativePath(specification.context)) || specification.arguments.some(argument =>
          typeof argument.type === 'object' && containsNativePath(argument.value)))
        fail('native path cannot be a Swift/context or buried POD argument');
      const slots = specification.arguments.map((argument, index) => argument.type === 'pointer' && argument.value &&
        argument.value.native_path !== undefined ? index : -1).filter(index => index >= 0);
      if (slots.length === 0) return null;
      if (specification.abi !== 'default' || specification.context !== undefined || specification.compiler_adapter !== undefined)
        fail('native path requires the existing ordinary direct C/POD route');
      const token = Object.freeze({});
      const call = {specification, snapshot: pathFreeze(pathCopy(specification)), argumentObjects: specification.arguments.slice(),
        invocation, prepared: new Map(), leases: [], permits: [], sealAttempted: false, dispatchAttempted: false,
        function: null, arguments: null};
      nativePathCalls.set(token, call);
      for (const argument of slots)
        call.prepared.set(argument, prepareNativePath(specification.arguments[argument].value.native_path, {call: token, argument}));
      return token;
    }
    function bindNativePathCall(token, function_, arguments_) {
      const call = nativePathCalls.get(token);
      if (!call || call.function !== null || !dataEqual(call.specification, call.snapshot)) fail('native path actual call changed');
      call.function = function_; call.arguments = arguments_;
    }
    function resolveNativePath(source, binding) {
      const call = binding.call === undefined ? null : nativePathCalls.get(binding.call);
      if (call === undefined) fail('native path actual call binding is unknown');
      const prepared = call === null ? prepareNativePath(source, binding) : call.prepared.get(binding.argument);
      if (!prepared || prepared.source !== source || call && call.argumentObjects[binding.argument] !== nativePathArgumentObject(call, binding.argument))
        fail('native path lease does not belong to the actual call object/slot');
      assertNativePathResources(prepared);
      if (receiverHypothesisV2(prepared.recipe)) {
        const token = Object.freeze({});
        const scope = {transaction: nativePathTransaction, prepared, phase: 'acquiring', getterEpoch: null,
          expectedEpoch: 1, expectedArgsLength: 0,
          specification: prepared.recipe.root.getter, function: null, pointer: null, observation: null,
          operation: activeOperation, operationSnapshot: pathFreeze(pathCopy(activeOperation)), index: activeOperationIndex,
          use: 'dispatch'};
        prepared.borrowedScope = token;
        borrowedRootScopes.set(token, scope);
        let pointer = callNative(scope.specification, false, undefined, token).value;
        if (!pointer || pointer.isNull() || !pointer.and(ptr(7)).isNull() || !scope.pointer || scope.pointer.compare(pointer) !== 0)
          fail('V2 borrowed root getter returned null, misaligned or differs from its captured raw return');
        readable(pointer, 8);
        assertBorrowedRootCurrent(prepared);
        const rootPointer = pointer, hopSteps = [];
        let lastGetterPointer = pointer;
        const isVtableSlot = prepared.recipe.consumer.dispatch === 'vtable_slot';
        const noHops = prepared.recipe.hops.length === 0;
        const hops = [{index: 0, kind: 'root_getter', name: 'root', non_null_verified: true,
          vptr_checked: noHops, vtable_slot_verified: noHops && isVtableSlot, this_adjusted_verified: noHops}];
        for (let idx = 0; idx < prepared.recipe.hops.length; idx++) {
          const hop = prepared.recipe.hops[idx], inputPtr = pointer;
          assertBorrowedRootCurrent(prepared);
          if (hop.kind === 'step_getter') {
            if (!inputPtr || inputPtr.isNull() || !inputPtr.and(ptr(7)).isNull())
              fail('V2 step_getter input pointer is null or misaligned');
            readable(inputPtr, 8);
            const spec = {kind: 'native_function', target: hop.target, abi: hop.abi,
              arguments: [{type: 'pointer', value: inputPtr}], return_type: 'pointer',
              signature_evidence: hop.signature_evidence};
            const found = nativeFunction(spec);
            scope.phase = 'acquiring';
            scope.function = found.function_;
            scope.expectedArgsLength = 1;
            scope.expectedEpoch += 1;
            send({event: 'native-getter-start', function: found.metadata});
            pointer = targetCall(found.function_, [inputPtr], null, token);
            send({event: 'native-getter-returned'});
            if (!pointer || pointer.isNull() || !pointer.and(ptr(7)).isNull() || !scope.pointer || scope.pointer.compare(pointer) !== 0)
              fail('V2 step_getter hop returned null, misaligned or differs from its captured raw return');
            readable(pointer, 8);
            lastGetterPointer = pointer;
            hopSteps.push({hop, inputPtr, outputPtr: pointer, slot: null, vectorBegin: null, vectorEnd: null, vectorCap: null});
          } else {
            const resolvedHop = resolveReceiverHypothesisV2Hop(inputPtr, hop);
            pointer = resolvedHop.pointer;
            hopSteps.push({hop, inputPtr, outputPtr: pointer, slot: resolvedHop.slot,
              vectorBegin: resolvedHop.vectorBegin, vectorEnd: resolvedHop.vectorEnd, vectorCap: resolvedHop.vectorCap});
          }
          const isTerminal = (idx + 1) === prepared.recipe.hops.length;
          hops.push({index: idx + 1, kind: hop.kind, name: hop.name, non_null_verified: true,
            vptr_checked: isTerminal, vtable_slot_verified: isTerminal && isVtableSlot,
            this_adjusted_verified: isTerminal});
        }
        const terminalReceiverPtr = pointer;
        verifyReceiverHypothesisV2VptrAndSlot(terminalReceiverPtr, prepared);
        const adjustedPointer = terminalReceiverPtr.add(prepared.recipe.consumer.this_adjustment || 0);
        if (adjustedPointer.isNull() || !adjustedPointer.and(ptr(7)).isNull())
          fail('V2 this_adjustment produced a null or misaligned receiver pointer');
        readable(adjustedPointer, 8);
        const currentScope = assertBorrowedRootCurrent(prepared);
        const observation = {recipe: pathCopy(prepared.recipe), argument: binding.argument,
          consumer: pathConsumerContractV2(prepared.recipe.consumer),
          root_kind: 'getter', owner_retained: false, queue: prepared.recipe.lifetime.queue || 'main',
          lifetime_mode: RECEIVER_HYPOTHESIS_V2_MODE,
          resolved: true, sealed: false, dispatch_permit_consumed: false, hops, expires: 'transaction',
          evidence_basis: {...RECEIVER_HYPOTHESIS_V2_BASIS},
          borrowed_scope: {getter_epoch: currentScope.getterEpoch, checked_epoch: null, use: 'dispatch',
            consumed: false, expires: 'use'}};
        currentScope.observation = observation;
        const leaseToken = Object.freeze({});
        nativePathLeases.set(leaseToken, {prepared, root: {owner: null, reads: [], pointer: lastGetterPointer},
          v2: {rootPointer, hopSteps, terminalReceiverPtr, adjustedPointer},
          pointers: [adjustedPointer], snapshots: [], observation, call: binding.call,
          argument: binding.argument, attempted: false});
        if (call !== null) call.leases.push(leaseToken);
        nativePaths.push(observation);
        send({event: 'native-path-resolved', observation});
        return {pointer: adjustedPointer, lease: leaseToken};
      }
      const root = {owner: null, reads: []};
      let pointer;
      if (prepared.recipe.lifetime.mode === 'main_queue_observed_root') {
        const observer = assertObservedCurrent(prepared);
        if (observer.acquired || observer.scope !== null) fail('observed root capability was already acquired');
        observer.acquired = true;
        pointer = observer.selected.pointer;
        observer.epoch = targetCallEpoch;
        observer.acquireEvent = observer.event('acquire', observer.selected.ordinal, null, null, observer.owner);
      } else if (['main_queue_borrowed_root', RECEIVER_HYPOTHESIS_MODE].includes(prepared.recipe.lifetime.mode)) {
        const token = Object.freeze({});
        const scope = {transaction: nativePathTransaction, prepared, phase: 'acquiring', getterEpoch: null,
          specification: prepared.recipe.root.getter, function: null, pointer: null, observation: null,
          operation: activeOperation, operationSnapshot: pathFreeze(pathCopy(activeOperation)), index: activeOperationIndex,
          use: call === null ? 'read' : 'dispatch'};
        prepared.borrowedScope = token;
        borrowedRootScopes.set(token, scope);
        pointer = callNative(scope.specification, false, undefined, token).value;
        if (!scope.pointer || scope.pointer.compare(pointer) !== 0) fail('borrowed root differs from its captured raw return');
      } else if (prepared.recipe.root.data !== undefined) {
        const data = prepared.recipe.root.data, location = nativePathDataRootLocation(prepared);
        root.data = Object.freeze({base: location.image.module.base, address: location.address});
        pointer = data.mode === 'object' ? location.address : readable(location.address, 8).readPointer();
        if (data.mode === 'pointer') root.reads.push({slot: location.address, type: 'pointer', value: pointer});
      } else {
        try { pointer = pointerValue(prepared.recipe.root, null, root); }
        catch (_error) { fail('native path existing root resolution failed; no retry'); }
      }
      root.pointer = pointer;
      assertNativePathRoot(root, prepared);
      const pointers = [], snapshots = [], seen = new Set(), hops = [];
      for (let hop = 0; hop < prepared.reached.length; hop++) {
        const layout = prepared.reached[hop], key = pointer.toString();
        if (seen.has(key)) fail('native path revisited a live address; cycles refused');
        seen.add(key);
        const values = nativePathSnapshot(pointer, layout, hop, prepared);
        pointers.push(pointer); snapshots.push(values);
        const edge = hop === prepared.recipe.path.length ? null : pathField(layout, prepared.recipe.path[hop]);
        hops.push({type: layout.name, pointer: holdPointer(pointer),
          guards: prepared.recipe.preconditions.filter(guard => guard.hop === hop).map(guard => ({field: guard.field,
            type: pathField(layout, guard.field).type, equals: guard.equals, matched: true})),
          edge: edge === null ? null : {field: edge.name, target_type: edge.target, unchanged: false},
          identity_checked: layout.runtime_identity !== undefined});
        if (edge !== null) pointer = values.get(edge.name);
      }
      const observation = {recipe: pathCopy(prepared.recipe), argument: call === null ? null : binding.argument,
        consumer: call === null ? null : pathConsumerContract(prepared.recipe.consumer, receiverHypothesis(prepared.recipe)),
        root_kind: Object.keys(prepared.recipe.root)[0],
        owner_retained: root.owner !== null, queue: 'main', lifetime_mode: prepared.recipe.lifetime.mode,
        resolved: true, sealed: false, dispatch_permit_consumed: false, hops, expires: 'transaction'};
      if (receiverHypothesis(prepared.recipe)) observation.evidence_basis = {...RECEIVER_HYPOTHESIS_BASIS};
      if (['main_queue_borrowed_root', RECEIVER_HYPOTHESIS_MODE].includes(prepared.recipe.lifetime.mode)) {
        const scope = assertBorrowedRootCurrent(prepared);
        observation.borrowed_scope = {getter_epoch: scope.getterEpoch, checked_epoch: null, use: scope.use,
          consumed: false, expires: 'use'};
        scope.observation = observation;
      }
      if (prepared.recipe.lifetime.mode === 'main_queue_observed_root') {
        const observer = assertObservedCurrent(prepared);
        observation.lifecycle_scope = {observer_sha256: observer.digest, generation: observer.selected.ordinal,
          constructor_enter_event: observer.selected.enter, constructor_return_event: observer.selected.returned,
          selection_event: observer.selectionEvent, acquire_event: observer.acquireEvent, consume_event: null,
          owner_thread_id: observer.owner, acquire_epoch: observer.epoch, checked_epoch: null,
          use: call === null ? 'read' : 'dispatch', consumed: false, expires: 'use'};
        observer.scope = observation.lifecycle_scope;
      }
      const token = Object.freeze({});
      nativePathLeases.set(token, {prepared, root, pointers, snapshots, observation, call: binding.call,
        argument: binding.argument, attempted: false});
      if (call !== null) call.leases.push(token);
      nativePaths.push(observation);
      send({event: 'native-path-resolved', observation});
      return {pointer, lease: token};
    }
    function sealNativePaths(token) {
      // Re-reading detects drift; it does not establish pointee lifetime. The
      // reviewed routes require original lifetime evidence. The separate binary
      // hypothesis tier assumes lifetime; these checks cannot turn it into proof.
      // No child retain or root getter reacquisition is performed here.
      const call = nativePathCalls.get(token);
      const allowArm64e = Boolean(call && call.prepared && Array.from(call.prepared.values()).some(p => v2AllowsArm64e(p.recipe)));
      assertNativePathCurrent(allowArm64e);
      if (!call || call.sealAttempted || !call.function || !call.arguments ||
          call.leases.length !== call.prepared.size || !dataEqual(call.specification, call.snapshot))
        fail('native path call lease is stale, mutated or already sealed');
      call.sealAttempted = true;
      for (const key of call.leases) {
        const lease = nativePathLeases.get(key);
        if (!lease || lease.attempted || lease.call !== token) fail('native path lease seal cannot be reused or forged');
        assertBorrowedRootCurrent(lease.prepared);
        lease.attempted = true;
        assertPathConsumer(lease.prepared.recipe, call.specification, lease.argument);
        assertNativePathResources(lease.prepared);
        if (lease.v2 !== undefined) {
          readable(lease.v2.rootPointer, 8);
          let expectedInput = lease.v2.rootPointer;
          for (const step of lease.v2.hopSteps) {
            if (step.inputPtr.compare(expectedInput) !== 0)
              fail('V2 native receiver hop input chain drifted before seal; no reacquisition');
            readable(step.inputPtr, 8);
            if (step.hop.kind !== 'step_getter') {
              const recheckedHop = resolveReceiverHypothesisV2Hop(step.inputPtr, step.hop);
              if (recheckedHop.pointer.compare(step.outputPtr) !== 0 ||
                  recheckedHop.slot.compare(step.slot) !== 0 ||
                  (step.vectorBegin !== null && (
                    recheckedHop.vectorBegin === null ||
                    recheckedHop.vectorBegin.compare(step.vectorBegin) !== 0 ||
                    recheckedHop.vectorEnd.compare(step.vectorEnd) !== 0 ||
                    recheckedHop.vectorCap.compare(step.vectorCap) !== 0)))
                fail('V2 native receiver pointer chain or container header drifted before seal; no reacquisition');
            }
            readable(step.outputPtr, 8);
            expectedInput = step.outputPtr;
          }
          if (expectedInput.compare(lease.v2.terminalReceiverPtr) !== 0)
            fail('V2 native receiver terminal pointer drifted before seal; no reacquisition');
          verifyReceiverHypothesisV2VptrAndSlot(lease.v2.terminalReceiverPtr, lease.prepared);
          const recheckedAdjusted = lease.v2.terminalReceiverPtr.add(lease.prepared.recipe.consumer.this_adjustment || 0);
          if (recheckedAdjusted.compare(lease.v2.adjustedPointer) !== 0)
            fail('V2 adjusted receiver pointer drifted before seal');
          readable(recheckedAdjusted, 8);
          const permit = Object.freeze({});
          nativePathPermits.set(permit, {transaction: nativePathTransaction, call: token, specification: call.specification,
            argument: lease.argument, lease: key, pointer: lease.v2.adjustedPointer, used: false});
          call.permits.push(permit);
          lease.observation.sealed = true;
          continue;
        }
        assertNativePathRoot(lease.root, lease.prepared);
        for (let hop = 0; hop < lease.pointers.length; hop++) {
          const actual = nativePathSnapshot(lease.pointers[hop], lease.prepared.reached[hop], hop, lease.prepared);
          for (const [name, wanted] of lease.snapshots[hop]) {
            const value = actual.get(name), field = pathField(lease.prepared.reached[hop], name);
            if (field.type === 'pointer' ? value.compare(wanted) !== 0 : typeof value !== typeof wanted || value !== wanted)
              fail('native path selected pointer/guard changed; no reacquisition');
          }
        }
        const permit = Object.freeze({});
        nativePathPermits.set(permit, {transaction: nativePathTransaction, call: token, specification: call.specification,
          argument: lease.argument, lease: key, pointer: lease.pointers[lease.pointers.length - 1], used: false});
        call.permits.push(permit);
        lease.observation.sealed = true;
        for (const hop of lease.observation.hops) if (hop.edge !== null) hop.edge.unchanged = true;
      }
    }
    function consumeNativePathPermits(token, function_, arguments_) {
      const call = nativePathCalls.get(token);
      const allowArm64e = Boolean(call && call.prepared && Array.from(call.prepared.values()).some(p => v2AllowsArm64e(p.recipe)));
      assertNativePathCurrent(allowArm64e);
      if (!call || call.dispatchAttempted) fail('native path dispatch permit is stale or already attempted');
      if (call.qtMoc) {
        assertQtMocCallCurrent(call);
        if (arguments_.length !== 4 || arguments_[1] !== 0 || arguments_[2] !== call.specification.selection.local_index ||
            arguments_[3] !== call.qtMoc.vector || function_ !== call.qtMoc.found.function_)
          fail('Qt one-shot permit is not bound to its private vector and selected callback');
      }
      call.dispatchAttempted = true;
      if (!call.sealAttempted || call.function !== function_ || call.arguments !== arguments_ ||
          !dataEqual(call.specification, call.snapshot) || call.permits.length !== call.prepared.size ||
          new Set(call.permits).size !== call.permits.length) fail('native path dispatch is not bound to this actual call');
      const admitted = [];
      for (const key of call.permits) {
        const permit = nativePathPermits.get(key), lease = permit && nativePathLeases.get(permit.lease);
        if (!permit || permit.used || permit.transaction !== nativePathTransaction || permit.call !== token ||
            permit.specification !== call.specification || !lease || lease.call !== token || lease.argument !== permit.argument ||
            nativePathArgumentObject(call, permit.argument) !== call.argumentObjects[permit.argument] ||
            arguments_[permit.argument].compare(permit.pointer) !== 0)
          fail('native path one-shot permit/slot/transaction binding was forged or changed');
        assertBorrowedRootCurrent(lease.prepared);
        admitted.push(permit);
      }
      for (const permit of admitted) {
        const lease = nativePathLeases.get(permit.lease);
        if (['main_queue_borrowed_root', RECEIVER_HYPOTHESIS_MODE, RECEIVER_HYPOTHESIS_V2_MODE].includes(lease.prepared.recipe.lifetime.mode))
          consumeBorrowedRoot(lease.prepared, 'dispatch');
        if (lease.prepared.recipe.lifetime.mode === 'main_queue_observed_root') consumeObservedRoot(lease.prepared, 'dispatch');
        permit.used = true; lease.observation.dispatch_permit_consumed = true;
      }
      return call.invocation;
    }
    function nativeReadRoot(specification) {
      if (specification.native_path !== undefined) return pointerValue(specification, {investigation: true});
      if (specification.native !== undefined) return pointerValue({getter: specification.native});
      return pointerValue(specification);
    }
    function nativeGraph(operation) {
      const layouts = new Map(operation.types.map(layout => [layout.name, layout]));
      const seen = new Map(), nodes = [], edges = [], errors = [];
      let truncated = false;
      function visit(pointer, type, depth, lineage) {
        deadline();
        if (pointer.isNull()) return null;
        const key = pointer.toString() + '/' + type;
        if (seen.has(key)) return seen.get(key);
        if (nodes.length >= operation.max_nodes) { truncated = true; return null; }
        const layout = layouts.get(type);
        const id = 'g' + (nodes.length + 1);
        const node = {id, type, depth, lineage, handle: holdPointer(pointer), fields: []};
        seen.set(key, id); nodes.push(node);
        try { readable(pointer, layout.bytes); }
        catch (error) { errors.push({node: id, error: errorInfo(error)}); return id; }
        for (const field of layout.fields) {
          deadline();
          try {
            const value = memoryField(pointer, field);
            const row = {name: field.name, type: field.type, offset: field.offset,
              value: serializeNative(value, field.type), observation: 'declared_memory_read'};
            node.fields.push(row);
            if (field.target !== undefined && !value.isNull()) {
              const edge = {from: id, field: field.name, target_type: field.target, to: null, cycle: seen.has(value.toString() + '/' + field.target)};
              if (depth >= operation.max_depth) { edge.truncated = true; truncated = true; }
              else edge.to = visit(value, field.target, depth + 1, lineage.concat([{field: field.name, offset: field.offset, type: field.target}]));
              edges.push(edge);
            }
          } catch (error) { errors.push({node: id, field: field.name, error: errorInfo(error)}); }
        }
        return id;
      }
      const callsBefore = result.native_calls;
      const root = visit(nativeReadRoot(operation.root), operation.root_type, 0, [{root: operation.root}]);
      return {root, nodes, edges, errors, truncated, getter_calls: result.native_calls - callsBefore,
        potentially_mutating_getters: result.native_calls > callsBefore, evidence_only: true, verified: false};
    }
    function objcField(receiver, field) {
      let value, metadata;
      if (field.kind === 'getter') {
        const found = methodOf(receiver, field.selector, field.signature, [], field.type);
        value = targetCall(found.method.bind(receiver), []);
        metadata = found.metadata;
      } else {
        if (receiver.$kind !== 'instance') fail('ivar read requires an instance');
        metadata = ivarEncoding(receiver, field.ivar);
        const encoded = tokens(metadata.encoding);
        if (encoded.length !== 1 || !compatible(field.type, encoded[0]) ||
            (field.encoding !== undefined && metadata.encoding !== field.encoding)) fail('runtime ivar type differs');
        value = receiver.$ivars[field.ivar];
      }
      return {value, metadata};
    }
    function objcGraph(operation) {
      const layouts = new Map(operation.types.map(layout => [layout.class, layout]));
      const seen = new Map(), nodes = [], edges = [], errors = [];
      let truncated = false;
      function visit(receiver, type, depth, lineage) {
        deadline();
        if (!receiver || receiver.handle.isNull()) return null;
        if (receiver.$kind !== 'instance') fail('graph roots/edges must reach actual objects');
        if (receiver.$className !== type) fail('reached object class differs from declared graph edge');
        const key = receiver.handle.toString();
        if (seen.has(key)) return seen.get(key);
        if (nodes.length >= operation.max_nodes) { truncated = true; return null; }
        const id = 'g' + (nodes.length + 1);
        const node = {id, class: type, depth, lineage, object: hold(receiver), fields: [], methods: []};
        seen.set(key, id); nodes.push(node);
        const methods = receiver.$methods;
        node.methods = methods.slice(0, operation.methods_limit).map(key => {
          const method = receiver[key];
          const owner = Process.findModuleByAddress(method.implementation);
          return {selector: key.slice(2), dispatch: key[0], signature: method.types,
            module: owner ? owner.path : null, implementation_offset: owner ? method.implementation.sub(owner.base).toString() : null,
            origin: methodOrigin(owner)};
        });
        node.methods_truncated = methods.length > node.methods.length;
        for (const field of layouts.get(type).fields) {
          deadline();
          try {
            const observed = objcField(receiver, field);
            node.fields.push({name: field.name, kind: field.kind, type: field.type,
              value: serialize(observed.value, field.type), metadata: observed.metadata,
              observation: field.kind === 'getter' ? 'executed_getter' : 'declared_ivar_read'});
            if (field.target !== undefined && observed.value && observed.value.handle && !observed.value.handle.isNull()) {
              const edge = {from: id, field: field.name, target_class: field.target, to: null,
                cycle: seen.has(observed.value.handle.toString()), kind: field.kind, metadata: observed.metadata};
              if (depth >= operation.max_depth) { edge.truncated = true; truncated = true; }
              else edge.to = visit(observed.value, field.target, depth + 1,
                lineage.concat([{field: field.name, kind: field.kind, selector: field.selector || null,
                  ivar: field.ivar || null, metadata: observed.metadata}]));
              edges.push(edge);
            }
          } catch (error) {
            errors.push({node: id, field: field.name, error: errorInfo(error)});
            if (result.native_state_uncertain) throw error;
          }
        }
        return id;
      }
      const callsBefore = result.native_calls;
      const receiver = receiverOf(operation.root);
      const root = visit(receiver, receiver.$className, 0, [{root: operation.root}]);
      return {root, nodes, edges, errors, truncated, getter_calls: result.native_calls - callsBefore,
        potentially_mutating_getters: result.native_calls > callsBefore, evidence_only: true, verified: false};
    }
    function execute(operation, index) {
      deadline();
      activeOperation = operation; activeOperationIndex = index;
      send({event: 'operation-start', index, op: operation.op});
      const row = {index, op: operation.op, status: 'ok'};
      const pathsBefore = nativePaths.length;
      if (operation.op === 'classes') {
        const lower = (operation.query || '').toLowerCase();
        const limit = operation.limit || MAX_ROWS;
        const matches = [];
        if (operation.owner === 'all') {
          // ModuleMap enumeration excludes runtime-generated classes whose names
          // and metadata live on the heap. The class registry includes them.
          for (const name of Object.keys(ObjC.classes).sort()) {
            if (!name.toLowerCase().includes(lower)) continue;
            const owner = classModule(classNamed(name));
            matches.push({name, module: owner, origin: owner === null ? 'runtime_generated' : 'module_backed'});
          }
        } else {
          const moduleMap = new ModuleMap(module => module.path.startsWith(payload.bundle + '/') ||
            module.path === payload.executable);
          const classes = ObjC.enumerateLoadedClassesSync({ownedBy: moduleMap});
          for (const owner of Object.keys(classes).sort()) {
            for (const name of classes[owner].sort()) {
              if (name.toLowerCase().includes(lower)) matches.push({name, module: owner, origin: 'module_backed'});
            }
          }
        }
        row.rows = matches.slice(0, limit);
        row.matched = matches.length;
        row.truncated = matches.length > row.rows.length;
      } else if (operation.op === 'methods') {
        const klass = classNamed(operation.class);
        const lower = (operation.query || '').toLowerCase();
        const methods = (operation.own === false ? klass.$methods : klass.$ownMethods).filter(name => name.toLowerCase().includes(lower)).sort();
        row.rows = methods.slice(0, operation.limit || MAX_ROWS).map(name => {
          const method = klass[name];
          const owner = Process.findModuleByAddress(method.implementation);
          return {selector: name.slice(2), dispatch: name[0], signature: method.types,
            class: klass.$className, module: owner ? owner.path : null,
            implementation_offset: owner ? method.implementation.sub(owner.base).toString() : null,
            origin: methodOrigin(owner)};
        });
        row.matched = methods.length;
        row.truncated = methods.length > row.rows.length;
      } else if (operation.op === 'ivars') {
        Object.assign(row, ivarsFor(classNamed(operation.class), operation.own !== false,
          operation.query || '', operation.limit || MAX_ROWS));
      } else if (operation.op === 'instances') {
        const object = unique(classNamed(operation.class), operation.subclasses === true);
        row.object = hold(object, operation.alias);
      } else if (operation.op === 'read') {
        const receiver = receiverOf(operation.receiver);
        if (operation.expected_class !== undefined && receiver.$className !== operation.expected_class)
          fail('read receiver class differs from the frozen expected class');
        if (!Array.isArray(operation.fields) || operation.fields.length > MAX_FIELDS) fail('read field budget exceeded');
        row.fields = [];
        for (const field of operation.fields) {
          deadline();
          const {value, metadata} = objcField(receiver, field);
          row.fields.push({name: field.name, kind: field.kind, type: field.type,
            value: serialize(value, field.type, field.alias), metadata});
        }
      } else if (operation.op === 'invoke') {
        const receiver = receiverOf(operation.receiver);
        const found = methodOf(receiver, operation.selector, operation.signature, operation.arguments, operation.return_type);
        const arguments_ = operation.arguments.map(argumentValue);
        deadline(); // A queued callback is never allowed to invoke after its deadline.
        send({event: 'invoke-start', index, method: found.metadata});
        result.invocations++;
        const value = targetCall(found.method.bind(receiver), arguments_);
        row.method = found.metadata;
        row.return_type = operation.return_type;
        row.value = serialize(value, operation.return_type, operation.alias);
        send({event: 'invoke-returned', index});
      } else if (operation.op === 'native_invoke') {
        const observed = callNative(operation, true, operation.alias);
        row.function = observed.function;
        row.return_type = observed.return_type;
        row.value = observed.serialized;
        row.out_buffers = observed.out_buffers;
        row.pointer_reads = observed.pointer_reads;
        row.swift_field_reads = observed.swift_field_reads;
        row.callable_entries = observed.callable_entries;
        row.compiler_completion = observed.compiler_completion;
      } else if (operation.op === 'qt_moc_invoke') {
        if (!qtMocOperationTokens.has(operation)) {
          if (operation.receiver.native_path.lifetime.mode !== 'main_queue_borrowed_root')
            fail('constructor-observed Qt operation lost its prepared observer');
          prepareQtMocCall(operation);
        }
        Object.assign(row, callQtMoc(operation));
        row.value = row.serialized;
        delete row.serialized;
      } else if (operation.op === 'resolve_callable') {
        row.callable = callablePointer(operation.entry).observation;
      } else if (operation.op === 'load_helper') {
        row.helper = loadHelper(operation);
      } else if (operation.op === 'modules') {
        const query = (operation.query || '').toLowerCase();
        const matches = Process.enumerateModules().filter(module =>
          (operation.owner === 'all' || module.path.startsWith(payload.bundle + '/') || module.path === payload.executable) &&
          (module.name.toLowerCase().includes(query) || module.path.toLowerCase().includes(query)));
        row.rows = matches.slice(0, operation.limit || MAX_ROWS).map(module =>
          ({name: module.name, path: module.path, bytes: module.size, identity: 'loaded_image_path', signature_inferred: false}));
        row.matched = matches.length;
        row.truncated = row.rows.length < matches.length;
      } else if (operation.op === 'symbols') {
        const resolved = moduleFor(operation.module), module = resolved.module;
        const query = (operation.query || '').toLowerCase();
        const symbols = operation.kind === 'exports' ? module.enumerateExports() : module.enumerateSymbols();
        const matches = symbols.filter(symbol => symbol.name.toLowerCase().includes(query));
        row.rows = matches.slice(0, operation.limit || MAX_ROWS).map(symbol => {
          const range = Process.findRangeByAddress(symbol.address);
          let owned = false;
          try { inImage(resolved, symbol.address, 1); owned = true; } catch (_error) { /* Outside this exact declared image. */ }
          let section = null;
          try { if (owned) section = instructionFor(resolved, symbol.address, (payload.native_resources || {})[module.path]); }
          catch (_error) { /* Data metadata on an RX page is inspectable, not callable. */ }
          return {name: symbol.name, kind: symbol.type, global: symbol.isGlobal === undefined ? null : symbol.isGlobal,
            module: module.path, implementation_offset: owned ? symbol.address.sub(module.base).toString() : null,
            executable: range !== null && range.protection.includes('x'), size: symbol.size === undefined ? null : symbol.size,
            callable: section !== null && range !== null && range.protection.includes('x'), instruction_section: section,
            prototype: null, signature_inferred: false};
        });
        row.module = operation.module;
        row.image_identity = resolved.identity;
        row.matched = matches.length; row.truncated = matches.length > row.rows.length;
      } else if (operation.op === 'swift_types') {
        row.rows = []; row.errors = []; row.truncated = false;
        if (Process.arch !== 'arm64' || !Swift.available) {
          row.unsupported = 'Swift metadata bridge requires ARM64 Darwin and loaded Swift runtime';
          row.matched = 0;
        } else {
          const query = (operation.query || '').toLowerCase();
          const images = operation.image === undefined ? Process.enumerateModules().filter(module =>
            module.path.startsWith(payload.bundle + '/') || module.path === payload.executable).map(module => ({module})) : [moduleFor(operation.image)];
          let matched = 0;
          for (const selected of images) {
            deadline();
            let image, declarations;
            try { image = selected.identity === undefined ? imageFor(selected.module) : selected; declarations = nominalDescriptors(image); }
            catch (error) { row.errors.push({image: selected.module.path, error: errorInfo(error)}); continue; }
            for (const info of declarations) {
              if (info.error !== undefined) {
                if (row.errors.length < MAX_ROWS) row.errors.push({image: image.module.path, descriptor_offset: info.descriptor.sub(image.module.base).toString(), error: info.error});
                continue;
              }
              if ((operation.module !== undefined && info.moduleName !== operation.module) || !info.name.toLowerCase().includes(query)) continue;
              matched++;
              if (row.rows.length >= (operation.limit || MAX_ROWS)) continue;
              const item = {source: 'swift_metadata', module: info.moduleName, image: image.module.path,
                image_identity: image.identity, name: info.name, category: {16: 'classes', 17: 'structs', 18: 'enums'}[info.kind],
                descriptor_offset: info.descriptor.sub(image.module.base).toString(), generic: info.generic,
                fields: [], methods: [], signature_inferred: false, fields_truncated: false,
                methods_unsupported: 'method ABI inference is not provided by declaration inventory'};
              if (info.generic) item.unsupported = 'generic context requires explicit runtime specialization';
              else {
                try {
                  const records = fieldRecords(info);
                  item.fields = records.slice(0, MAX_FIELDS).map(record => ({name: record.name, mutable: record.mutable,
                    type_name: null, mangled_type_offset: record.typePointer.sub(image.module.base).toString(),
                    type_ref_hex: Array.from(new Uint8Array(record.typePointer.readByteArray(record.length))).map(byte => byte.toString(16).padStart(2, '0')).join('')}));
                  item.fields_truncated = records.length > item.fields.length;
                } catch (error) { item.unsupported = error.message; }
              }
              row.rows.push(item);
            }
          }
          row.matched = matched; row.truncated = matched > row.rows.length;
        }
      } else if (operation.op === 'swift_fields') {
        const reflection = classReflection(operation), query = (operation.query || '').toLowerCase();
        const matches = reflection.records.filter(record => record.name.toLowerCase().includes(query));
        row.source = 'swift_metadata'; row.receiver_class = reflection.receiver.$className;
        row.swift_class = {module: reflection.info.moduleName, name: reflection.info.name};
        row.image_identity = reflection.image.identity; row.module_size = reflection.image.extent;
        row.reported_module_size = reflection.image.module.size;
        row.instance_bytes = reflection.instanceBytes; row.class_size = reflection.classSize; row.class_address_point = reflection.classAddressPoint;
        row.num_fields = reflection.numFields; row.vector_offset_words = reflection.vector;
        row.descriptor_offset = reflection.info.descriptor.sub(reflection.image.module.base).toString();
        row.rows = [];
        for (const record of matches.slice(0, operation.limit || MAX_ROWS)) {
          const item = {name: record.name, index: record.index, offset: reflection.offsets[record.index], mutable: record.mutable,
            mangled_type_offset: record.typePointer.sub(reflection.image.module.base).toString(), type_name: null,
            type_ref_hex: Array.from(new Uint8Array(record.typePointer.readByteArray(record.length))).map(byte => byte.toString(16).padStart(2, '0')).join('')};
          try {
            const field = reflectedField(reflection, record);
            item.type_name = field.typeName; item.layout = field.layout;
            if (field.typeName === null) item.unsupported = 'resolved field is outside the canonical supported value subset';
          } catch (error) {
            if (result.native_state_uncertain) throw error;
            item.unsupported = error.message;
          }
          row.rows.push(item);
        }
        row.matched = matches.length; row.truncated = matches.length > row.rows.length;
      } else if (operation.op === 'swift_read') {
        row.observation = swiftFieldRead(operation.read, false).observation;
      } else if (operation.op === 'native_read') {
        const resolved = operation.receiver.native_path === undefined ? null :
          resolveNativePath(operation.receiver.native_path, {investigation: true, operation});
        const prepared = resolved === null ? null : nativePathLeases.get(resolved.lease).prepared;
        const pointer = resolved === null ? nativeReadRoot(operation.receiver) : resolved.pointer;
        if (prepared !== null) assertBorrowedRootCurrent(prepared);
        readable(pointer, operation.bytes);
        row.object = holdPointer(pointer); row.fields = [];
        for (const field of operation.fields) {
          deadline();
          if (prepared !== null) assertBorrowedRootCurrent(prepared);
          const value = memoryField(pointer, field);
          row.fields.push({name: field.name, type: field.type, offset: field.offset,
            value: serializeNative(value, field.type, field.alias), observation: 'declared_memory_read'});
        }
        if (prepared !== null && prepared.recipe.lifetime.mode === 'main_queue_borrowed_root') consumeBorrowedRoot(prepared, 'read');
        if (prepared !== null && prepared.recipe.lifetime.mode === 'main_queue_observed_root') consumeObservedRoot(prepared, 'read');
      } else if (operation.op === 'graph') {
        Object.assign(row, operation.root.native === undefined && operation.root.native_path === undefined
          ? objcGraph(operation) : nativeGraph(operation));
      } else fail('unsupported fixed runtime operation');
      if (nativePaths.length > pathsBefore) row.native_paths = nativePaths.slice(pathsBefore);
      result.operations.push(row);
      send({event: 'operation-returned', index, op: operation.op});
      if (row.compiler_completion !== undefined && row.compiler_completion !== null &&
          (!row.compiler_completion.valid || row.compiler_completion.status !== 0))
        fail('compiler adapter refused or failed; completion retained and later operations stopped');
    }
    function makeLifecycleObserver(prepared) {
      const resolved = lifecycleSpecification(prepared.recipe), spec = resolved.spec;
      const owner = Process.getCurrentThreadId();
      if (!Number.isSafeInteger(owner) || owner < 1 || targetCallEpoch !== 0)
        fail('lifecycle observer needs a fresh actual macOS main-thread marker and zero target epoch');
      const record = result.lifecycle_observer = {format: 'anyuse.rea-native-lifecycle-observation/1',
        observer_sha256: Checksum.compute('sha256', canonicalJSON(spec)), status: 'incomplete', owner_thread_id: owner,
        selected_generation: null, events: [], hooks_requested: resolved.destructors.length + 1,
        hook_attempts: 0, hooks_installed: 0, hooks_detached: 0, listeners_cleanup_flushed: false,
        callbacks_inflight: 0, lifecycle_frames_inflight: 0, poisoned: false, tokens_expired: false};
      const observer = {recipe: prepared.recipe, prepared, operation: activeOperation,
        operationSnapshot: pathFreeze(pathCopy(activeOperation)), owner, digest: record.observer_sha256,
        phase: 'installing', poisoned: false, callbacks: 0, pendingCallbacks: 0, frames: [], generations: new Map(),
        addresses: new Map(), listeners: [], timer: null, selected: null, selectionEvent: null,
        acquireEvent: null, epoch: 0, scope: null, acquired: false, consumed: false};
      observer.assertLocations = () => {
        const actual = lifecycleSpecification(prepared.recipe);
        if (actual.constructor.compare(resolved.constructor) !== 0 ||
            actual.returns.some((address, index) => address.compare(resolved.returns[index]) !== 0) ||
            actual.destructors.some((address, index) => address.compare(resolved.destructors[index]) !== 0))
          fail('lifecycle observer original code image/base changed after hook installation');
      };
      observer.event = (kind, generation = null, entry = null, returnSite = null, thread = null) => {
        const limit = kind === 'stop' ? spec.event_limit : spec.event_limit - 1;
        if (record.events.length >= limit) fail('lifecycle private event budget exhausted');
        const seq = record.events.length + 1;
        record.events.push({seq, kind, generation, entry, return_site: returnSite, thread});
        return seq;
      };
      observer.live = () => Array.from(observer.generations.values()).filter(row => row.state === 'live');
      observer.poison = error => {
        if (!observer.poisoned) {
          observer.poisoned = true; record.poisoned = true;
          try { observer.event('poison'); } catch (_error) { /* Never truncate or reset the ledger. */ }
          if (result.error === null) result.error = errorInfo(error);
        }
      };
      observer.callback = work => {
        if (['stopping', 'stopped'].includes(observer.phase)) return;
        observer.callbacks++;
        try {
          if (observer.poisoned) return;
          if (!['observing', 'selected', 'using', 'consumed'].includes(observer.phase))
            fail('lifecycle event arrived before complete arming');
          deadline();
          work();
        } catch (error) { observer.poison(error); }
        finally { observer.callbacks--; }
      };
      const thread = invocation => {
        if (invocation.threadId !== observer.owner || Process.getCurrentThreadId() !== observer.owner)
          fail('lifecycle event is not on the independently admitted main owner thread');
        return observer.owner;
      };
      const pointer = args => {
        const value = args[0].add(0);
        if (value.isNull()) fail('lifecycle entry supplied a null primary this');
        return value;
      };
      const install = (address, callbacks) => {
        record.hook_attempts++;
        // Use the ordinary checked online installation. No custom redirect,
        // replacement, unchecked relocation, forced target or app code change.
        const listener = Interceptor.attach(address, callbacks);
        if (!listener || typeof listener.detach !== 'function') fail('checked lifecycle listener did not install');
        observer.listeners.push(listener);
        record.hooks_installed++;
      };
      observer.install = () => {
        for (let index = 0; index < resolved.destructors.length; index++) {
          install(resolved.destructors[index], {
            onEnter(args) { observer.callback(() => {
              const tid = thread(this), object = pointer(args), generation = observer.addresses.get(object.toString()) || null;
              const frame = {kind: 'destructor', generation, entry: index};
              this.lifecycleFrame = frame;
              if (generation !== null) {
                generation.state = 'dead';
                if (generation === observer.selected && !observer.consumed)
                  fail('selected observed generation was destroyed before use');
              }
              observer.frames.push(frame);
              observer.event('destructor_enter', generation === null ? null : generation.ordinal, index, null, tid);
            }); },
            onLeave() { observer.callback(() => {
              const tid = thread(this), frame = this.lifecycleFrame;
              if (!frame || observer.frames[observer.frames.length - 1] !== frame || frame.kind !== 'destructor')
                fail('lifecycle destructor completion is unmatched or out of order');
              observer.frames.pop();
              observer.event('destructor_leave', frame.generation === null ? null : frame.generation.ordinal, index, null, tid);
            }); }
          });
        }
        install(resolved.constructor, {
          onEnter(args) { observer.callback(() => {
            const tid = thread(this), object = pointer(args);
            if (observer.frames.length || observer.phase !== 'observing')
              fail('nested or post-selection constructor cannot create an observed root');
            const previous = observer.addresses.get(object.toString());
            if (previous && previous.state !== 'dead') fail('lifecycle address was reused without witnessed destruction');
            const site = resolved.returns.findIndex(address => address.compare(this.returnAddress) === 0);
            let generation = null;
            if (site >= 0) {
              if (observer.generations.size >= spec.generation_limit) fail('lifecycle private generation budget exhausted');
              generation = {ordinal: observer.generations.size + 1, pointer: object, state: 'constructing', enter: null, returned: null};
              observer.generations.set(generation.ordinal, generation);
              observer.addresses.set(object.toString(), generation);
            }
            const frame = {kind: 'constructor', generation, entry: 0, site: site < 0 ? null : site};
            this.lifecycleFrame = frame; observer.frames.push(frame);
            const seq = observer.event('constructor_enter', generation === null ? null : generation.ordinal, 0, frame.site, tid);
            if (generation !== null) generation.enter = seq;
          }); },
          onLeave() { observer.callback(() => {
            const tid = thread(this), frame = this.lifecycleFrame;
            if (!frame || observer.frames[observer.frames.length - 1] !== frame || frame.kind !== 'constructor')
              fail('lifecycle constructor completion is unmatched or out of order');
            observer.frames.pop();
            const generation = frame.generation;
            if (generation !== null && generation.state !== 'constructing') fail('invalidated construction cannot become live');
            const seq = observer.event('constructor_return', generation === null ? null : generation.ordinal, 0, frame.site, tid);
            if (generation !== null) { generation.state = 'live'; generation.returned = seq; }
          }); }
        });
        Interceptor.flush();
        observer.phase = 'observing';
        observer.event('armed', null, null, null, owner);
      };
      observer.select = () => {
        if (observer.poisoned || observer.phase !== 'observing' || observer.callbacks || observer.frames.length)
          fail('lifecycle observation ended poisoned or during an unfinished phase');
        deadline(); assertNativePathResources(prepared); observer.assertLocations();
        if (Date.now() + 3000 > payload.deadline_ms) fail('lifecycle observation left no admitted use/cleanup reserve');
        observer.event('observation_end');
        const candidates = observer.live();
        if (candidates.length !== 1) fail('lifecycle observation needs exactly one fresh live generation');
        observer.selected = candidates[0]; record.selected_generation = observer.selected.ordinal;
        observer.selectionEvent = observer.event('selected', observer.selected.ordinal);
        observer.phase = 'selected';
      };
      observer.stop = () => {
        observer.phase = 'stopping';
        if (observer.timer !== null) {
          try { clearTimeout(observer.timer); }
          catch (error) { observer.poison(error); result.native_state_uncertain = true; }
          observer.timer = null;
        }
        // Expire capability ownership before touching listeners. Frame counts
        // are retained, never reset merely because observation stopped.
        record.callbacks_inflight = observer.callbacks + observer.pendingCallbacks;
        record.lifecycle_frames_inflight = observer.frames.length;
        observer.scope = null;
        for (const generation of observer.generations.values()) { generation.state = 'expired'; generation.pointer = null; }
        observer.addresses.clear(); observer.generations.clear(); observer.selected = null;
        record.tokens_expired = true;
        for (const listener of observer.listeners.slice().reverse()) {
          try { listener.detach(); record.hooks_detached++; }
          catch (error) { observer.poison(error); result.native_state_uncertain = true; }
        }
        try { Interceptor.flush(); record.listeners_cleanup_flushed = true; }
        catch (error) { observer.poison(error); result.native_state_uncertain = true; }
        try { observer.event('stop'); } catch (error) { observer.poison(error); }
        if (record.callbacks_inflight || record.lifecycle_frames_inflight || record.hooks_detached !== record.hooks_requested ||
            record.hooks_installed !== record.hooks_requested || record.hook_attempts !== record.hooks_requested ||
            !record.listeners_cleanup_flushed || observer.poisoned || !observer.consumed) {
          result.status = 'incomplete';
          if (record.callbacks_inflight || record.lifecycle_frames_inflight || record.hook_attempts !== record.hooks_installed)
            result.native_state_uncertain = true;
        }
        record.status = result.status === 'completed' ? 'completed' : 'incomplete';
        observer.phase = 'stopped';
      };
      return observer;
    }
    function observedPathSources(value, rows = []) {
      if (value === null || typeof value !== 'object') return rows;
      if (value.native_path && value.native_path.root && value.native_path.root.lifecycle !== undefined) {
        rows.push(value.native_path);
        return rows;
      }
      for (const child of Object.values(value)) observedPathSources(child, rows);
      return rows;
    }
    function finishTransaction(resolve) {
      if (phasePermit !== null) {
        try { phasePermit.close(); }
        catch (error) { result.status = 'incomplete';
          if (result.error === null) result.error = errorInfo(error); }
      }
      if (lifecycleObserver !== null) {
        try { lifecycleObserver.stop(); }
        catch (error) { result.status = 'incomplete'; result.native_state_uncertain = true;
          if (result.error === null) result.error = errorInfo(error); }
      }
      nativePathActive = false;
      for (const call of nativePathCalls.values()) if (call.qtMoc) {
        call.qtMoc.vector = null;
        call.qtMoc.expired = true;
      }
      qtMocOperationTokens = new WeakMap();
      nativePathPermits.clear(); nativePathLeases.clear(); nativePathCalls.clear(); borrowedRootScopes.clear();
      activeOperation = null; activeOperationIndex = -1; nativePaths.length = 0;
      if (nativePathQueueOwner === nativePathTransaction) nativePathQueueOwner = null;
      aliases.clear(); nativeAliases.clear(); borrowed.clear(); pointerReads.length = 0;
      swiftFieldReads.length = 0; callableEntries.length = 0; sdkReturnStorage = new WeakMap();
      sdkAllocationOwners.length = 0; sdkAllocationBytes = 0; allocations.length = 0;
      for (const entry of Array.from(retained.values()).reverse()) {
        try { entry.object['- release'](); }
        catch (error) { result.status = 'incomplete'; result.native_state_uncertain = true; result.error = {...errorInfo(error), type: 'LifetimeError'}; }
      }
      retained.clear(); result.handles_expired = true;
      resolve(result);
    }
    if (receiverHypothesisRequested) {
      try { assertReceiverRequest(); }
      catch (error) { result.error = errorInfo(error); return new Promise(resolve => finishTransaction(resolve)); }
    }
    const observedSources = observedPathSources(request);
    if (phasePermitRequested && observedSources.length !== 1) fail('private phase permit requires one lifecycle-observed receiver');
    if (observedSources.length) return new Promise(resolve => {
      let finished = false;
      const finish = () => { if (!finished) { finished = true; finishTransaction(resolve); } };
      const failed = error => { if (lifecycleObserver !== null) lifecycleObserver.poison(error);
        if (result.error === null) result.error = errorInfo(error); finish(); };
      const terminal = () => {
        if (lifecycleObserver.pendingCallbacks > 0) lifecycleObserver.pendingCallbacks--;
        if (finished) return;
        try {
          nativePathActive = true;
          lifecycleObserver.phase = 'using';
          assertObservedCurrent(lifecycleObserver.prepared);
          execute(request.operations[0], 0);
          deadline();
          const call = ['native_invoke', 'qt_moc_invoke'].includes(request.operations[0].op);
          if (!lifecycleObserver.consumed || result.native_calls !== Number(call) || result.invocations !== Number(call))
            fail('observed terminal lost its single-use explicit-call disposition');
          result.status = 'completed';
        } catch (error) { lifecycleObserver.poison(error); if (result.error === null) result.error = errorInfo(error); }
        finally { finish(); }
      };
      if (phasePermitRequested) {
        try { phasePermit = makePhasePermit(payload.pre_actuation_ack, failed); }
        catch (error) { failed(error); return; }
      }
      try { ObjC.schedule(ObjC.mainQueue, () => {
        try {
          deadline();
          if (sdkAllocationCaptureOwner !== null || nativePathQueueOwner !== null || observedSources.length !== 1 || request.operations.length !== 1)
            fail('observed root requires exclusive single-operation transaction ownership');
          nativePathActive = true; nativePathQueueOwner = nativePathTransaction;
          activeOperation = request.operations[0]; activeOperationIndex = 0;
          let prepared;
          if (activeOperation.op === 'native_read') prepared = prepareNativePath(observedSources[0],
            {investigation: true, operation: activeOperation});
          else if (activeOperation.op === 'native_invoke') {
            const token = prepareNativePathCall(activeOperation, true);
            const call = nativePathCalls.get(token);
            if (!call || call.prepared.size !== 1 || !call.prepared.has(0)) fail('observed root requires the sole direct this slot');
            prepared = call.prepared.get(0);
          } else if (activeOperation.op === 'qt_moc_invoke') {
            const token = prepareQtMocCall(activeOperation), call = nativePathCalls.get(token);
            if (!call || call.prepared.size !== 1 || !call.prepared.has(0)) fail('Qt observer lost its sole primary receiver slot');
            prepared = call.prepared.get(0);
          } else fail('observed root permits only one native scalar read or direct this call');
          const spec = prepared.recipe.root.lifecycle;
          if (Date.now() + spec.observe_ms + 3000 > payload.deadline_ms)
            fail('lifecycle observation does not fit the supplied overall deadline');
          lifecycleObserver = makeLifecycleObserver(prepared);
          lifecycleObserver.install();
          if (phasePermit !== null) { phasePermit.bind(prepared); phasePermit.arm(); }
          nativePathActive = false;
          lifecycleObserver.timer = setTimeout(() => {
            if (finished) return;
            try {
              nativePathActive = true;
              lifecycleObserver.select();
              nativePathActive = false;
              const scheduleTerminal = () => {
                if (finished) return;
                lifecycleObserver.pendingCallbacks++;
                ObjC.schedule(ObjC.mainQueue, terminal);
              };
              if (phasePermit === null) scheduleTerminal();
              else phasePermit.wait(lifecycleObserver).then(allowed => { if (allowed) scheduleTerminal(); }).catch(failed);
            } catch (error) { failed(error); }
          }, spec.observe_ms);
          send({event: 'lifecycle-observer-ready', observe_ms: spec.observe_ms, hooks: lifecycleObserver.listeners.length});
        } catch (error) { failed(error); }
      }); } catch (error) { failed(error); }
    });
    send({event: 'main-queue-scheduled', deadline_ms: payload.deadline_ms});
    return new Promise(resolve => {
      ObjC.schedule(ObjC.mainQueue, () => {
        try {
          deadline();
          if (sdkAllocationCaptureOwner !== null) fail('queued transaction cannot enter a Swift SDK allocation capture');
          if (nativePathQueueOwner !== null) fail('queued transaction cannot enter another borrowed native path lifetime');
          nativePathActive = true;
          if (containsNativePath(request)) nativePathQueueOwner = nativePathTransaction;
          for (let index = 0; index < request.operations.length; index++) execute(request.operations[index], index);
          deadline();
          if (receiverHypothesisRequested) {
            const obs = nativePaths.length === 1 ? nativePaths[0] : null;
            const isV1 = obs && receiverHypothesis(obs.recipe);
            const isV2 = obs && receiverHypothesisV2(obs.recipe);
            const expectedCalls = isV2 ? obs.recipe.lifetime.expected_native_calls : 2;
            const expectedEpoch = expectedCalls - 1;
            if (!obs || (!isV1 && !isV2) || result.native_calls !== expectedCalls || result.invocations !== 1 ||
                targetCallEpoch !== expectedCalls || !obs.sealed || !obs.dispatch_permit_consumed ||
                !obs.borrowed_scope || !obs.borrowed_scope.consumed ||
                obs.borrowed_scope.getter_epoch !== expectedEpoch || obs.borrowed_scope.checked_epoch !== expectedEpoch)
              fail('receiver hypothesis lost its exact getter and immediate one-shot consumer disposition');
          }
          result.status = 'completed';
        } catch (error) {
          result.error = errorInfo(error);
        } finally {
          finishTransaction(resolve);
        }
      });
    });
  }
};
