#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0
fail=0

for f in "$ROOT"/tests/pos/*.mnd; do
    exp="$f.out"
    if out="$("$ROOT/bin/mandela" run "$f" 2>&1)" && [ "$out" = "$(cat "$exp")" ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "FAIL pos: $f"
        echo "--- expected ---"
        cat "$exp"
        echo "--- got ---"
        printf '%s\n' "$out"
    fi
done

for f in "$ROOT"/tests/neg/*.mnd; do
    exp="$f.err"
    if out="$("$ROOT/bin/mandela" check "$f" 2>&1)"; then
        fail=$((fail + 1))
        echo "FAIL neg (accepted invalid code): $f"
    elif printf '%s' "$out" | grep -qF "$(cat "$exp")"; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "FAIL neg: $f"
        echo "--- expected to contain ---"
        cat "$exp"
        echo "--- got ---"
        printf '%s\n' "$out"
    fi
done

echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
