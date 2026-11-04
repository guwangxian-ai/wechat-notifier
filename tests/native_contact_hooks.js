// SPDX-License-Identifier: GPL-3.0-or-later
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = fs.readFileSync(process.argv[2], 'utf8');
const start = script.indexOf('function contactAttach(');
const end = script.indexOf('function contactAesDecrypt(', start);
assert.ok(start >= 0 && end > start);
const setup = script.slice(start, end);
const names = ['CCCryptorCreate','CCCryptorCreateWithMode','CCCrypt','CCKeyDerivationPBKDF'];
for (const failures of [[], [names[0]], names]) {
  const installed = [];
  const context = {
    Process: {arch:'x64'}, contactDirectoryEnabled: true,
    contactState: {installed:false, inCrypto:false},
    File: {}, SqliteDatabase: {}, console: {log() {}},
    contactFindExport(name) {return name;},
    NativeFunction: function () {return function () {};},
    Interceptor: {attach(name) {
      if (failures.includes(name)) throw new Error('unable to intercept function');
      installed.push(name);
    }},
  };
  vm.runInNewContext(setup+'\ncontactSetupHooks();',context);
  assert.equal(installed.length,names.length-failures.length);
  assert.equal(context.contactState.installed,failures.length<names.length);
}
let installedAt = null;
const context = {
  Process:{arch:'arm64'}, contactState:{},
  Instruction:{parse(address) {assert.equal(address.offset,4);}},
  Interceptor:{attach(address) {installedAt=address.offset;}},
};
vm.runInNewContext(setup,context);
const entry = {offset:0,strip() {return this;},readU32() {return 0xdac1a7fe;},add(n) {return {offset:n};}};
assert.equal(context.contactAttach(entry,{onEnter() {}}),true);
assert.equal(installedAt,4);
installedAt=null;
assert.equal(context.contactAttach(entry,{onEnter() {},onLeave() {}}),false);
assert.equal(installedAt,null);
console.log('Contact Hook isolation and PACIBSPPC guards: 5 scenarios passed');
