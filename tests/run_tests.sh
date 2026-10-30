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

# native codegen: every positive test must produce identical output when
# compiled to a native executable via `mandela build`
CC_BIN="${CC:-cc}"
if command -v "$CC_BIN" >/dev/null 2>&1; then
    for f in "$ROOT"/tests/pos/*.mnd; do
        bin="$(mktemp /tmp/mandela_native.XXXXXX)"
        if build_out="$("$ROOT/bin/mandela" build "$f" -o "$bin" 2>&1)"; then
            if nat_out="$("$bin" 2>&1)" && [ "$nat_out" = "$(cat "$f.out")" ]; then
                pass=$((pass + 1))
            else
                fail=$((fail + 1))
                echo "FAIL native: $f"
                echo "--- expected ---"
                cat "$f.out"
                echo "--- got ---"
                printf '%s\n' "$nat_out"
            fi
        else
            fail=$((fail + 1))
            echo "FAIL native build: $f"
            printf '%s\n' "$build_out"
        fi
        rm -f "$bin"
    done
else
    echo "note: no C compiler found, skipping native build tests"
fi

echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
