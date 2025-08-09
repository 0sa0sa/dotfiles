autoload -Uz colors
colors

autoload -U compinit
compinit

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

export LSCOLORS=exfxcxdxbxegedabagacad
export LS_COLORS='di=34:ln=35:so=32:pi=33:ex=31:bd=46;34:cd=43;34:su=41;30:sg=46;30:tw=42;30:ow=43;30'

alias c="claude"
alias cdan="claude --dangerously-skip-permissions"

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

# alias kexec="kubectl exec --stdin --tty $1 -- /bin/bash"
alias kk="kubectl kustomize"
alias ka="kubectl apply"
alias k="kubectl"
alias ksp="k get pods -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | fzf"

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
export PATH="$HOME/.local/bin:$PATH"

eval "$(starship init zsh)"
eval $(thefuck --alias)
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

# The next line updates PATH for the Google Cloud SDK.
if [ -f '~/Downloads/google-cloud-sdk/path.zsh.inc' ]; then . '~/Downloads/google-cloud-sdk/path.zsh.inc'; fi

# The next line enables shell command completion for gcloud.
if [ -f '~/Downloads/google-cloud-sdk/completion.zsh.inc' ]; then . '~/Downloads/google-cloud-sdk/completion.zsh.inc'; fi

# Add fpath for completions
fpath=(~/.zsh/completions $fpath)

# Source worktree-manager script
source ~/.zsh/worktree-manager.zsh
source <(kubectl completion zsh)
eval "$(direnv hook zsh)"
. "$HOME/.local/bin/env"
