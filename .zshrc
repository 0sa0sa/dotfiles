
autoload -Uz colors
colors

autoload -U compinit

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

alias glog="git log --oneline --decorate --graph --all"
alias gs="git status"
alias gss="git status -s"
alias ga="git add"
alias gc="git commit"

alias gch="git checkout"
alias gp="git pull"
alias gpod="git pull orign develop"

alias d="docker"
alias di="docker image"
alias dis="docker images"
alias dc="docker container"

alias b="bpctl"
# alias kexec="kubectl exec --stdin --tty $1 -- /bin/bash"
alias kk="kubectl kustomize"
alias ka="kubectl apply"
alias k="kubectl"

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
PATH="/usr/local/opt/gnu-tar/libexec/gnubin:$PATH"
PATH="/usr/local/opt/gnu-sed/libexec/gnubin:$PATH"
eval "$(starship init zsh)"


eval "$(anyenv init -)"


eval $(thefuck --alias)
