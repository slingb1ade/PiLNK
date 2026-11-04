#!/bin/bash
# Runs every tests/image/test_*.sh (bash) and test_*.py (python3). Each test
# prints "PASS: name" / "FAIL: name" lines; a test file that exits non-zero
# counts as one extra failure. Prints "---- N passed, M failed"; exit 1 on any.
cd "$(dirname "$0")" || exit 1
pass=0; fail=0
for t in test_*.sh test_*.py; do
  [ -e "$t" ] || continue
  echo "== $t"
  if [[ "$t" == *.py ]]; then out=$(python3 "$t" 2>&1); rc=$?; else out=$(bash "$t" 2>&1); rc=$?; fi
  printf '%s\n' "$out"
  p=$(printf '%s\n' "$out" | grep -c '^PASS:'); f=$(printf '%s\n' "$out" | grep -c '^FAIL:')
  pass=$((pass + p)); fail=$((fail + f))
  if [ "$rc" -ne 0 ]; then echo "FAIL: $t exited $rc"; fail=$((fail + 1)); fi
done
echo "---- $pass passed, $fail failed"
[ "$fail" -eq 0 ]
