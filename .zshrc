autoload -Uz colors
colors

# Completions
fpath=(~/.zsh/completions $fpath)
autoload -U compinit
compinit

# Worktree manager
source ~/.zsh/functions/w.zsh

autoload predict-on
predict-on

setopt correct

setopt auto_cd
function chpwd() {ls}

# alias npm='pnpm'
alias p='pnpm'
alias run='pnpm run'

alias ls='ls -G'
alias lla='ls -laG'

alias awk='gawk'

alias pon='predict-on'
alias poff='predict-off'
alias tailscale="/Applications/Tailscale.app/Contents/MacOS/Tailscale"

export LSCOLORS=exfxcxdxbxegedabagacad
export LS_COLORS='di=34:ln=35:so=32:pi=33:ex=31:bd=46;34:cd=43;34:su=41;30:sg=46;30:tw=42;30:ow=43;30'

# alias c="claude"  # commented out to use function instead
#alias cdan="claude --dangerously-skip-permissions"

alias codan="codex --dangerously-bypass-approvals-and-sandbox"

alias glog="git log --oneline --decorate --graph --all"
alias gs="git status"
alias gss="git switch staging"
alias gsm="git switch main"
alias ga="git add"
alias gc="git commit"

alias gcb="git checkout -b"
alias gch="git checkout"
alias 'gc-'="git checkout -"
alias gcbfm='git fetch origin main && git checkout -b'
alias gbfm='git fetch origin main && git branch'
alias gbd="git branch -D"
alias gpull="git pull"
alias gpod="git pull origin develop"
alias gpom="git pull origin main"
alias gsm="git switch main"
alias gsd="git switch develop"
alias gpush="git push"
alias gsu="git stash -u"
alias 'git stash'='git stash -u'
alias gsp='git stash pop'
alias gslist='git stash list'
alias gr='git revert $(git log --oneline | fzf | awk "{print \$1}")'
alias grn='git revert -n $(git log --oneline | fzf | awk "{print \$1}")'
alias grskip='git revert --skip'
alias gw='git worktree'
alias gwl='git worktree list'
alias gwa='git worktree add'
alias gwr='git worktree remove $(git worktree list | fzf | awk "{print \$1}")'

alias d="docker"
alias di="docker image"
alias dis="docker images"
alias dc="docker container"


alias t="terraform"

#alias ls='ls -G'
alias lla='ls -laG'

# exaのalias area
if [[ $(command -v exa) ]]; then
  alias e='exa --icons --git'
  alias l=e
  alias ls=e
  alias ea='exa -a --icons --git'
  alias la=ea
  alias ee='exa -aahl --icons --git'
  alias ll=ee
  alias et='exa -T -L 3 -a -I "node_modules|.git|.cache" --icons'
  alias lt=et
  alias eta='exa -T -a -I "node_modules|.git|.cache" --color=always --icons | less -r'
  alias lta=eta
  alias l='clear && ls'
fi
 
set -o vi
bindkey "jj" vi-cmd-mode
#" bashの場合
#" bind '"jj": vi-movement-mode'
#
export VOLTA_HOME="$HOME/.volta"
export PATH="$VOLTA_HOME/bin:$HOME/.local/bin:$PATH"
export PATH="/usr/local/opt/gnu-tar/libexec/gnubin:$PATH"
export PATH="/usr/local/opt/gnu-sed/libexec/gnubin:$PATH"
export PATH="$HOME/.local/bin:/usr/local:$PATH"

eval "$(starship init zsh)"
eval "$(/opt/homebrew/bin/mise activate zsh)"

gsw() {
  git switch "$(
    git branch --sort=-committerdate --format="%(refname:short)  |  %(committerdate:relative) |  %(subject)" \
    | fzf \
    | awk '{print $1}'
  )"
}

rm() {
      for arg in "$@"; do
          case "$arg" in
              (/ | ~ | ~/ | ~/* | /* | *-/*)
                  echo "Refusing to rm '$arg'"
                  return 1
                  ;;
          esac
      done
      command rm "$@"
  }

autoload -U +X bashcompinit && bashcompinit
complete -o nospace -C /opt/homebrew/bin/terraform terraform

# Claude MCP config function
cdan() {
  if [ -f "./.mcp.json" ]; then
    command claude --dangerously-skip-permissions --mcp-config ./.mcp.json "$@"
  elif [ -f "$HOME/.mcp.json" ]; then
    command claude --dangerously-skip-permissions --mcp-config ~/.mcp.json "$@"
  else
    command claude --dangerously-skip-permissions "$@"
  fi
}

# herdr restore types `claude --resume <sid>` into the shell (hardcoded argv,
# herdr src/agent_resume.rs); wrap claude so --resume always gets yolo mode
claude() {
  if [[ " $* " == *" --resume"* && " $* " != *"--dangerously-skip-permissions"* ]]; then
    command claude "$@" --dangerously-skip-permissions
  else
    command claude "$@"
  fi
}
eval "$(direnv hook zsh)"
. "$HOME/.local/bin/env"


# pnpm
export PNPM_HOME="$HOME/Library/pnpm"
case ":$PATH:" in
  *":$PNPM_HOME:"*) ;;
  *) export PATH="$PNPM_HOME:$PATH" ;;
esac
# pnpm end
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"

# bun completions
[ -s "$HOME/.bun/_bun" ] && source "$HOME/.bun/_bun"

# bun
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"

# Added by Antigravity
export PATH="$HOME/.antigravity/antigravity/bin:$PATH"

# The next line updates PATH for the Google Cloud SDK.
if [ -f "$HOME/google-cloud-sdk/path.zsh.inc" ]; then . "$HOME/google-cloud-sdk/path.zsh.inc"; fi

# The next line enables shell command completion for gcloud.
if [ -f "$HOME/google-cloud-sdk/completion.zsh.inc" ]; then . "$HOME/google-cloud-sdk/completion.zsh.inc"; fi
