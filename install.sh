#!/bin/sh
# Install heavylane CLI and its agent skill; integrations are opt-in.
set -eu
claude_link=0
extra_link=0
for arg in "$@"; do
  case "$arg" in
    --claude) claude_link=1 ;;
    --hermes) extra_link=1 ;;
    -h|--help) echo 'Usage: ./install.sh [--claude] [--hermes]'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin" "$HOME/.agents/skills"
if [ -d "$HOME/.local/bin/heavylane" ]; then
  echo 'refusing to replace a directory at the CLI path' >&2; exit 1
fi
ln -sfn "$here/bin/heavylane" "$HOME/.local/bin/heavylane"
if [ -L "$HOME/.agents/skills/heavylane" ]; then
  rm "$HOME/.agents/skills/heavylane"
fi
rsync -a --delete "$here/skill/heavylane/" "$HOME/.agents/skills/heavylane/"
refresh_link() {
  target=$1
  mkdir -p "$(dirname "$target")"
  if [ -L "$target" ]; then
    rm "$target"
  elif [ -e "$target" ]; then
    echo "refusing to replace a real skill directory: $target" >&2; exit 1
  fi
  ln -s "$HOME/.agents/skills/heavylane" "$target"
}
if [ "$claude_link" = 1 ]; then refresh_link "$HOME/.claude/skills/heavylane"; fi
if [ "$extra_link" = 1 ]; then refresh_link "$HOME/.hermes/skills/heavylane"; fi
echo "heavylane: $HOME/.local/bin/heavylane (add ~/.local/bin to PATH)"
echo "skill: $HOME/.agents/skills/heavylane"
