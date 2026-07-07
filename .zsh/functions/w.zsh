#!/usr/bin/env zsh
# Multi-project worktree manager with Claude support
#
# ASSUMPTIONS & SETUP:
# - Your git projects live in: ~/projects/
# - Worktrees will be created in: ~/projects/worktrees/<project>/<branch>
# - New branches will be named: <your-username>/<feature-name>
# - Configuration in: ~/.zsh/w.conf
#
# DIRECTORY STRUCTURE EXAMPLE:
# ~/projects/
# ├── my-app/              (main git repo)
# ├── another-project/     (main git repo)
# └── worktrees/
#     ├── my-app/
#     │   ├── feature-x/   (worktree)
#     │   └── bugfix-y/    (worktree)
#     └── another-project/
#         └── new-feature/ (worktree)

# Load configuration
[[ -f "$HOME/.zsh/w.conf" ]] && source "$HOME/.zsh/w.conf"

# Initialize default config if not set
[[ -z "${W_ALIASES+x}" ]] && typeset -gA W_ALIASES
[[ -z "${W_COPY_FILES+x}" ]] && typeset -gA W_COPY_FILES
[[ -z "${W_SETUP_COMMANDS+x}" ]] && typeset -gA W_SETUP_COMMANDS
[[ -z "${W_ENTER_COMMANDS+x}" ]] && typeset -gA W_ENTER_COMMANDS

# Helper: Show usage
_w_usage() {
    cat << 'EOF'
Usage: w [options] [project] [worktree] [command...]

Options:
  -h, --help     Show this help message
  --list         List all worktrees
  --rm <p> <wt>  Remove a worktree

Arguments:
  project        Project name or alias (see aliases below)
  worktree       Worktree name (creates if not exists)
  command        Optional command to run in worktree

Examples:
  w                           # Interactive: select worktree with fzf
  w a feature-x               # cd to my-app/feature-x (creates if needed)
  w a feature-x claude        # Run claude in worktree
  w --rm a feature-x          # Remove worktree

Project Aliases:
EOF
    # Show configured aliases
    if (( ${#W_ALIASES} > 0 )); then
        for alias full in ${(kv)W_ALIASES}; do
            printf "  %-6s -> %s\n" "$alias" "$full"
        done
    else
        echo "  (none configured)"
    fi
}

# Helper: Resolve project alias to full name
_w_resolve_project() {
    local input="$1"
    # Check if it's an alias
    if [[ -n "${W_ALIASES[$input]}" ]]; then
        echo "${W_ALIASES[$input]}"
    else
        echo "$input"
    fi
}

# Helper: Get all worktrees as "project.worktree" list (dot separated)
_w_get_all_worktrees() {
    local projects_dir="$HOME/projects"
    local worktrees_dir="$HOME/projects/worktrees"
    local -a result

    # Check worktrees directory
    if [[ -d "$worktrees_dir" ]]; then
        for project in $worktrees_dir/*(/N); do
            local project_name=$(basename "$project")
            for wt in $project/*(/N); do
                result+=("$project_name.$(basename "$wt")")
            done
        done
    fi

    # Check legacy core-wts location
    if [[ -d "$projects_dir/core-wts" ]]; then
        for wt in $projects_dir/core-wts/*(/N); do
            result+=("core.$(basename "$wt")")
        done
    fi

    printf '%s\n' "${result[@]}"
}

# Helper: Run setup for a project (copy files, run commands)
_w_run_setup() {
    local project="$1"
    local wt_path="$2"
    local is_new="$3"  # "true" if newly created
    local projects_dir="$HOME/projects"

    # Copy files (only for new worktrees, don't overwrite existing)
    if [[ "$is_new" == "true" && -n "${W_COPY_FILES[$project]}" ]]; then
        echo "Setting up files for $project..."
        local file_pairs="${W_COPY_FILES[$project]}"
        for pair in ${(s: :)file_pairs}; do
            local src="${pair%%:*}"
            local dst="${pair#*:}"
            local src_path="$projects_dir/$project/$src"
            local dst_path="$wt_path/$dst"

            if [[ -f "$src_path" && ! -f "$dst_path" ]]; then
                cp "$src_path" "$dst_path"
                echo "  Copied: $src -> $dst"
            elif [[ -f "$dst_path" ]]; then
                echo "  Skipped (exists): $dst"
            elif [[ ! -f "$src_path" ]]; then
                echo "  Warning: Source not found: $src"
            fi
        done
    fi

    # Run setup commands (only for new worktrees)
    if [[ "$is_new" == "true" && -n "${W_SETUP_COMMANDS[$project]}" ]]; then
        echo "Running setup command for $project..."
        local cmd="${W_SETUP_COMMANDS[$project]}"
        (cd "$wt_path" && eval "$cmd")
    fi

    # Run enter commands (every time)
    if [[ -n "${W_ENTER_COMMANDS[$project]}" ]]; then
        local cmd="${W_ENTER_COMMANDS[$project]}"
        (cd "$wt_path" && eval "$cmd")
    fi
}

# Helper: List all worktrees (for --list)
_w_list() {
    local projects_dir="$HOME/projects"
    local worktrees_dir="$HOME/projects/worktrees"

    echo "=== All Worktrees ==="
    # Check new location
    if [[ -d "$worktrees_dir" ]]; then
        for project in $worktrees_dir/*(/N); do
            local project_name=$(basename "$project")
            echo "\n[$project_name]"
            for wt in $project/*(/N); do
                echo "  - $(basename "$wt")"
            done
        done
    fi
    # Also check old core-wts location
    if [[ -d "$projects_dir/core-wts" ]]; then
        echo "\n[core] (legacy location)"
        for wt in $projects_dir/core-wts/*(/N); do
            echo "  - $(basename "$wt")"
        done
    fi
}

# Helper: Interactive fzf selection
_w_fzf_select() {
    local worktrees
    worktrees=$(_w_get_all_worktrees)

    if [[ -z "$worktrees" ]]; then
        echo "No worktrees found."
        return 1
    fi

    local selected
    selected=$(echo "$worktrees" | fzf --height=40% --reverse --border --prompt="Select worktree: ")

    if [[ -z "$selected" ]]; then
        return 1
    fi

    # Parse selection: project.worktree (dot separated, first dot only)
    local project="${selected%%.*}"
    local worktree="${selected#*.}"

    # Call w with the selected project and worktree
    w "$project" "$worktree"
}

# Main function
w() {
    local projects_dir="$HOME/projects"
    local worktrees_dir="$HOME/projects/worktrees"

    # No arguments: interactive fzf selection
    if [[ $# -eq 0 ]]; then
        _w_fzf_select
        return $?
    fi

    # Handle flags
    case "$1" in
        -h|--help)
            _w_usage
            return 0
            ;;
        --list)
            _w_list
            return 0
            ;;
        --rm)
            shift
            local project="$(_w_resolve_project "$1")"
            local worktree="$2"
            if [[ -z "$project" || -z "$worktree" ]]; then
                echo "Usage: w --rm <project> <worktree>"
                return 1
            fi
            # Check both locations for core
            if [[ "$project" == "core" && -d "$projects_dir/core-wts/$worktree" ]]; then
                (cd "$projects_dir/$project" && git worktree remove "$projects_dir/core-wts/$worktree")
            else
                local wt_path="$worktrees_dir/$project/$worktree"
                if [[ ! -d "$wt_path" ]]; then
                    echo "Worktree not found: $wt_path"
                    return 1
                fi
                (cd "$projects_dir/$project" && git worktree remove "$wt_path")
            fi
            return $?
            ;;
    esac

    # Normal usage: w <project> <worktree> [command...]
    local project_input="$1"
    local worktree="$2"
    shift 2 2>/dev/null || {
        echo "Usage: w <project> <worktree> [command...]"
        echo "       w -h for more help"
        return 1
    }
    local command=("$@")

    # Resolve project alias
    local project="$(_w_resolve_project "$project_input")"

    if [[ -z "$worktree" ]]; then
        echo "Usage: w <project> <worktree> [command...]"
        echo "       w -h for more help"
        return 1
    fi

    # Check if project exists
    if [[ ! -d "$projects_dir/$project" ]]; then
        echo "Project not found: $projects_dir/$project"
        if [[ "$project" != "$project_input" ]]; then
            echo "(resolved from alias: $project_input -> $project)"
        fi
        return 1
    fi

    # Convert slash to dot for directory name (e.g., feature/hoge -> feature.hoge)
    local wt_dirname="${worktree//\//.}"

    # Determine worktree path - check multiple locations
    local wt_path=""
    local is_new="false"

    if [[ "$project" == "core" ]]; then
        # For core, check old location first
        if [[ -d "$projects_dir/core-wts/$wt_dirname" ]]; then
            wt_path="$projects_dir/core-wts/$wt_dirname"
        elif [[ -d "$worktrees_dir/$project/$wt_dirname" ]]; then
            wt_path="$worktrees_dir/$project/$wt_dirname"
        fi
    else
        # For other projects, check new location
        if [[ -d "$worktrees_dir/$project/$wt_dirname" ]]; then
            wt_path="$worktrees_dir/$project/$wt_dirname"
        fi
    fi

    # If worktree doesn't exist, create it
    if [[ -z "$wt_path" || ! -d "$wt_path" ]]; then
        echo "Creating new worktree: $worktree"
        is_new="true"

        # Ensure worktrees directory exists
        mkdir -p "$worktrees_dir/$project"

        # Determine branch name
        local branch_name="$worktree"

        # Create the worktree in new location
        wt_path="$worktrees_dir/$project/$wt_dirname"
        (
            cd "$projects_dir/$project"
            git fetch origin main
            # Try creating new branch from origin/main, if branch exists use existing branch
            if git show-ref --verify --quiet "refs/heads/$branch_name"; then
                echo "Branch '$branch_name' already exists, using existing branch"
                git worktree add "$wt_path" "$branch_name"
            else
                git worktree add "$wt_path" -b "$branch_name" origin/main
            fi
        ) || {
            echo "Failed to create worktree"
            return 1
        }
    fi

    # Execute based on number of arguments
    if [[ ${#command[@]} -eq 0 ]]; then
        # No command specified - run setup and cd to the worktree
        _w_run_setup "$project" "$wt_path" "$is_new"
        cd "$wt_path"
    else
        # Command specified - run it in the worktree without cd'ing
        # Skip setup when running a command (assumed to be a quick operation)
        local old_pwd="$PWD"
        cd "$wt_path"
        eval "${command[@]}"
        local exit_code=$?
        cd "$old_pwd"
        return $exit_code
    fi
}

# Setup completion
_setup_w_completion() {
    local completion_file="$HOME/.zsh/completions/_w"

    # Always regenerate to include aliases
    mkdir -p "$HOME/.zsh/completions"
    cat > "$completion_file" << 'COMPLETION_EOF'
#compdef w

_w() {
    local curcontext="$curcontext" state line
    typeset -A opt_args

    local projects_dir="$HOME/projects"
    local worktrees_dir="$HOME/projects/worktrees"

    # Load config for aliases
    [[ -f "$HOME/.zsh/w.conf" ]] && source "$HOME/.zsh/w.conf"
    [[ -z "${W_ALIASES+x}" ]] && typeset -gA W_ALIASES

    # Define the main arguments
    _arguments -C \
        '(-h --help)'{-h,--help}'[Show help]' \
        '(--rm)--list[List all worktrees]' \
        '(--list)--rm[Remove a worktree]' \
        '1: :->project' \
        '2: :->worktree' \
        '3: :->command' \
        '*:: :->command_args' \
        && return 0

    case $state in
        project)
            if [[ "${words[1]}" == "--list" || "${words[1]}" == "-h" || "${words[1]}" == "--help" ]]; then
                return 0
            fi

            # Get list of projects (directories in ~/projects that are git repos)
            local -a projects
            local -a aliases_desc

            for dir in $projects_dir/*(N/); do
                if [[ -d "$dir/.git" ]]; then
                    projects+=(${dir:t})
                fi
            done

            # Add aliases with descriptions
            for alias full in ${(kv)W_ALIASES}; do
                aliases_desc+=("$alias:-> $full")
            done

            _describe -t aliases 'project alias' aliases_desc
            _describe -t projects 'project' projects
            return 0
            ;;

        worktree)
            local project_input="${words[2]}"

            if [[ -z "$project_input" ]]; then
                return 0
            fi

            # Resolve alias
            local project="$project_input"
            if [[ -n "${W_ALIASES[$project_input]}" ]]; then
                project="${W_ALIASES[$project_input]}"
            fi

            local -a worktrees

            # For core project, check both old and new locations
            if [[ "$project" == "core" ]]; then
                if [[ -d "$projects_dir/core-wts" ]]; then
                    for wt in $projects_dir/core-wts/*(N/); do
                        worktrees+=(${wt:t})
                    done
                fi
                if [[ -d "$worktrees_dir/core" ]]; then
                    for wt in $worktrees_dir/core/*(N/); do
                        if [[ ! " ${worktrees[@]} " =~ " ${wt:t} " ]]; then
                            worktrees+=(${wt:t})
                        fi
                    done
                fi
            else
                if [[ -d "$worktrees_dir/$project" ]]; then
                    for wt in $worktrees_dir/$project/*(N/); do
                        worktrees+=(${wt:t})
                    done
                fi
            fi

            if (( ${#worktrees} > 0 )); then
                _describe -t worktrees 'existing worktree' worktrees
            else
                _message 'new worktree name'
            fi
            ;;

        command)
            local -a common_commands
            common_commands=(
                'claude:Start Claude Code session'
                'gst:Git status'
                'gaa:Git add all'
                'gcmsg:Git commit with message'
                'gp:Git push'
                'gco:Git checkout'
                'gd:Git diff'
                'gl:Git log'
                'npm:Run npm commands'
                'yarn:Run yarn commands'
                'pnpm:Run pnpm commands'
                'make:Run make commands'
            )

            _describe -t commands 'command' common_commands
            _command_names -e
            ;;

        command_args)
            words=(${words[4,-1]})
            CURRENT=$((CURRENT - 3))
            _normal
            ;;
    esac
}

_w "$@"
COMPLETION_EOF
}

# Run completion setup
_setup_w_completion
