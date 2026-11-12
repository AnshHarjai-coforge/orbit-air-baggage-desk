# STRETCH GOAL 1
# PreToolUse hook. Exit 2 when tool_input.file_path is an existing file inside
# this project's tests/ directory. Creating a new file there exits 0, and so
# does every path that is not inside that directory.
# Relative paths are resolved from CLAUDE_PROJECT_DIR, or from the repository
# that contains this script. "." and ".." are removed before the check, so a
# name like tests_backup/ or a path that climbs out of the project is allowed.

input=$(cat)

file_path=$(printf '%s' "$input" | awk '
  BEGIN { json = "" }
  { json = json $0 "\n" }
  END {
    key = "\"file_path\""
    i = index(json, key)
    if (i == 0) exit 0
    rest = substr(json, i + length(key))
    sub(/^[[:space:]]*:[[:space:]]*/, "", rest)
    if (substr(rest, 1, 1) != "\"") exit 0
    rest = substr(rest, 2)
    out = ""
    while (length(rest) > 0) {
      c = substr(rest, 1, 1)
      rest = substr(rest, 2)
      if (c == "\\") {
        e = substr(rest, 1, 1)
        rest = substr(rest, 2)
        if (e == "n") out = out "\n"
        else if (e == "t") out = out "\t"
        else if (e == "r") out = out "\r"
        else out = out e
      } else if (c == "\"") {
        break
      } else {
        out = out c
      }
    }
    printf "%s", out
  }
') || exit 0

[ -n "$file_path" ] || exit 0

if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  project=$CLAUDE_PROJECT_DIR
else
  hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
  project=$(CDPATH= cd -- "$hook_dir/../.." && pwd -P) || exit 0
fi
project=$(CDPATH= cd -P -- "$project" && pwd) || exit 0

case $file_path in
  /*) absolute=$file_path ;;
  *) absolute=$project/$file_path ;;
esac

# Drop "." and ".." without requiring the path to exist.
normalize_path() {
  path=$1
  set -f
  old_ifs=$IFS
  IFS=/
  # shellcheck disable=SC2086
  set -- $path
  IFS=$old_ifs
  set +f
  out=
  for part in "$@"; do
    case $part in
      ""|.)
        ;;
      ..)
        out=${out%/*}
        ;;
      *)
        out=$out/$part
        ;;
    esac
  done
  [ -n "$out" ] || out=/
  printf '%s\n' "$out"
}

# Follow symlinks on the part of the path that exists, then reattach the rest.
physical_path() {
  path=$1
  suffix=
  prev=
  while [ ! -d "$path" ]; do
    [ "$path" = / ] && break
    [ "$path" = "$prev" ] && break
    prev=$path
    base=${path##*/}
    path=${path%/*}
    [ -n "$path" ] || path=/
    if [ -n "$suffix" ]; then
      suffix=$base/$suffix
    else
      suffix=$base
    fi
  done
  if [ -d "$path" ]; then
    phys=$(CDPATH= cd -P -- "$path" && pwd) || return 1
  else
    phys=/
  fi
  if [ -n "$suffix" ]; then
    printf '%s\n' "$phys/$suffix"
  else
    # pwd of "/" is "/", so do not produce a doubled slash.
    printf '%s\n' "$phys"
  fi
}

logical=$(normalize_path "$absolute") || exit 0
candidate=$(physical_path "$logical") || exit 0
tests_dir=$(physical_path "$project/tests") || exit 0

case $candidate in
  "$tests_dir"|"$tests_dir"/*)
    ;;
  *)
    exit 0
    ;;
esac

if [ -e "$candidate" ] || [ -L "$candidate" ] || [ -e "$logical" ] || [ -L "$logical" ]; then
  printf '%s\n' "Blocked: refusing to edit existing test file: $file_path" >&2
  printf '%s\n' "Creating a new file under tests/ is allowed. Editing a file that already exists there is not." >&2
  exit 2
fi

exit 0
