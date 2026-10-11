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

alias ls='ls -G'
alias lla='ls -laG'

alias awk='gawk'

alias pon='predict-on'
alias poff='predict-off'

export LSCOLORS=exfxcxdxbxegedabagacad
export LS_COLORS='di=34:ln=35:so=32:pi=33:ex=31:bd=46;34:cd=43;34:su=41;30:sg=46;30:tw=42;30:ow=43;30'

# alias c="claude"  # commented out to use function instead

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

set -o vi
bindkey "jj" vi-cmd-mode

export PATH="$HOME/.local/bin:$PATH"

eval "$(starship init zsh)"
eval "$(direnv hook zsh)"
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
