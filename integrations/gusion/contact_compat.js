// SPDX-License-Identifier: GPL-3.0-or-later
// macOS arm64e.x1 uses PACIBSPPC (0xdac1a7fe). Keep that instruction at
// its original PC so the CPU signs LR normally. Only entry callbacks are
// supported after it; an onLeave callback could replace that signed LR.
// Validated on Mac mini: all four CommonCrypto APIs returned success with
// real calls using synthetic zero-value inputs. No protection is disabled.
function contactAttach(address, callbacks) {
    try {
        var site = address;
        if (Process.arch === "arm64") {
            var entry = address.strip();
            if (entry.readU32() === 0xdac1a7fe) {
                if (typeof callbacks.onLeave === "function") {
                    throw new Error("PACIBSPPC onLeave is unsupported");
                }
                site = entry.add(4);
                Instruction.parse(site);
            }
        }
        Interceptor.attach(site, callbacks);
        contactState.hookCount = (contactState.hookCount || 0) + 1;
        return true;
    } catch (e) {
        contactState.lastError = String(e);
        return false;
    }
}
