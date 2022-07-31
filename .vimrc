
" メモ
" https://www.youtube.com/watch?v=vEwyheX8gH4
" ss sv splitする vs sp
" sl sh sk sj で移動する
" s{char}をsearchではなく、moveのmに割り当てる。
"
"dein Scripts-----------------------------
if &compatible
  set nocompatible               " Be iMproved
endif

" reset augroup
augroup MyAutoCmd
  autocmd!
augroup END

" dein settings {{{
" dein自体の自動インストール
let s:cache_home = empty($XDG_CACHE_HOME) ? expand('~/.vim') : $XDG_CACHE_HOME
let s:dein_dir = s:cache_home . '/dein'
let s:dein_repo_dir = s:dein_dir . '/repos/github.com/Shougo/dein.vim'
if !isdirectory(s:dein_repo_dir)
  call system('git clone https://github.com/Shougo/dein.vim ' . shellescape(s:dein_repo_dir))
endif
let &runtimepath = s:dein_repo_dir .",". &runtimepath
" プラグイン読み込み＆キャッシュ作成
let s:toml_file = fnamemodify(expand('<sfile>'), ':h').'/dein.toml'
if dein#load_state(s:dein_dir)
  call dein#begin(s:dein_dir)
  call dein#load_toml(s:toml_file)
  call dein#end()
  call dein#save_state()
endif
" 不足プラグインの自動インストール
if has('vim_starting') && dein#check_install()
  call dein#install()
endif
" }}}/

" Required:
filetype plugin indent on
syntax enable

"End dein Scripts-------------------------


let @a="03wdei\<C-R>=2012-\<C-R>\"\<C-M>\<Esc>j"

"Setting---------------------------------
set wildmenu

set scrolloff=999

"set relativenumber
" set all で全てのオプションを表示できる
set number

set clipboard+=unnamed

set backspace=indent,eol,start

set incsearch

set hlsearch

" plugins
" startfy
let g:startify_files_number = 5
let g:startify_list_order = [
        \ ['♻  最近使ったファイル:'],
        \ 'files',
        \ ['♲  最近使ったファイル(カレントディレクトリ下):'],
        \ 'dir',
        \ ['⚑  セッション:'],
        \ 'sessions',
        \ ['☺  ブックマーク:'],
        \ 'bookmarks',
        \ ]

let g:startify_custom_header = [
\ '',
\ '                       __         _    _        _    _      _         _      ',
\ '                      / /    ___ | |_ ( ) ___  | |_ | |__  (_) _ __  | | __  ',
\ '                     / /    / _ \| __||/ / __| | __|| |_ \ | || |_ \ | |/ /  ',
\ '                    / /___ |  __/| |_    \__ \ | |_ | | | || || | | ||   <   ',
\ '                    \____/  \___| \__|   |___/  \__||_| |_||_||_| |_||_|\_\  ',
\ '                                                                             ',
\ '',
\]

" netrw---------------
filetype plugin on
" ファイルツリーの表示形式、1にするとls -laのような表示になります
let g:netrw_liststyle=1
" ヘッダを非表示にする
let g:netrw_banner=0
" サイズを(K,M,G)で表示する
let g:netrw_sizestyle="H"
" 日付フォーマットを yyyy/mm/dd(曜日) hh:mm:ss で表示する
let g:netrw_timefmt="%Y/%m/%d(%a) %H:%M:%S"
" プレビューウィンドウを垂直分割で表示する
let g:netrw_preview=1

" fernのアイコン表示
let g:fern#renderer = 'nerdfont'

" fernアイコンに色をつける
augroup my-glyph-palette
  autocmd! *
  autocmd FileType fern call glyph_palette#apply()
  autocmd FileType nerdtree,startify call glyph_palette#apply()
augroup END

"fern preview setting
function! s:fern_settings() abort
  nmap <silent> <buffer> p     <Plug>(fern-action-preview:toggle)
  nmap <silent> <buffer> <C-p> <Plug>(fern-action-preview:auto:toggle)
  nmap <silent> <buffer> <C-d> <Plug>(fern-action-preview:scroll:down:half)
  nmap <silent> <buffer> <C-u> <Plug>(fern-action-preview:scroll:up:half)
endfunction

augroup fern-settings
  autocmd!
  autocmd FileType fern call s:fern_settings()
augroup END


" previmのブラウザ指定
let g:previm_open_cmd = 'open -a Google\ Chrome'

"airline
"let g:airline_theme = 'wombat'               " テーマの指定
let g:airline_theme = 'ubaryd'               " テーマの指定
let g:airline#extensions#tabline#enabled = 1 " タブラインを表示
let g:airline_powerline_fonts = 1            " Powerline Fontsを利用
set ttimeoutlen=50



"" fzf.vim
" Ctrl+pでファイル検索を開く
" git管理されていれば:GFiles、そうでなければ:Filesを実行する
fun! FzfOmniFiles()
  let is_git = system('git status')
  if v:shell_error
    :Files
  else
    :GFiles
  endif
endfun
nnoremap <C-p> :call FzfOmniFiles()<CR>

" Ctrl+gで文字列検索を開く
" <S-?>でプレビューを表示/非表示する
command! -bang -nargs=* Rg
\ call fzf#vim#grep(
\ 'rg --column --line-number --hidden --ignore-case --no-heading --color=always '.shellescape(<q-args>), 1,
\ <bang>0 ? fzf#vim#with_preview({'options': '--delimiter : --nth 3..'}, 'up:60%')
\ : fzf#vim#with_preview({'options': '--exact --delimiter : --nth 3..'}, 'right:50%:hidden', '?'),
\ <bang>0)
nnoremap <C-g> :Rg<CR>


nnoremap <Space>f :Ex<CR>

"mapping------------------------------
let mapleader = "\<Space>"

inoremap <silent>jj <ESC>

nnoremap <silent> <Esc><Esc> :nohlsearch<CR>

" xやsでヤンクしないようにする
nnoremap x "_x
nnoremap s "_s

noremap j gj
noremap k gk
noremap <Leader>h ^
noremap <Leader>l $
noremap ; :
let g:EasyMotion_do_mapping = 0 "Disable default mappings
" Jump to first match whith enter & space
let g:EasyMotion_enter_jump_first = 1
let g:EasyMotion_space_jump_first = 1
let g:EasyMotion_smartcase = 1
"let g:EasyMotion_use_migemo = 1
" nmap S <Plug>(easymotion-s)
" nmap s <Plug>(easymotion-s2)
nmap s <Plug>(easymotion-overwin-f2)
vmap s <Plug>(easymotion-s)
map <Leader>j <Plug>(easymotion-j)
map <Leader>k <Plug>(easymotion-k)
map / <Plug>(easymotion-sn)
omap / <Plug>(easymotion-tn)
map <Leader>n <Plug>(easymotion-next)
map <Leader>N <Plug>(easymotion-prev)
" s<CR>で前回のs{char}{char}をリピートできる

" nmap g/ <Plug>(easymotion-sn)
map f <Plug>(easymotion-fl)
map t <Plug>(easymotion-tl)
map F <Plug>(easymotion-Fl)
map T <Plug>(easymotion-Tl)
"map f <Plug>(easymotion-bd-fl)
"map t <Plug>(easymotion-bd-tl)
"
nmap gh <Plug>AirlineSelectPrevTab
nmap gl <Plug>AirlineSelectNextTab

nnoremap <silent> <Leader>e :Fern . -drawer -width=40 -toggle<CR>

" fzfショートカット
nnoremap gr :Ag<CR>
