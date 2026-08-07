# bash completion for s1am
# Source this file or add to ~/.bash_completion.d/:
#   source /path/to/s1am-completion.bash

_s1am_global_opts="--version --profile --verbose --quiet --help"

_s1am_commands=(
  list-accounts get-account account-policy
  list-sites list-agents list-threats list-groups list-exclusions
  update-site create-site move-agents
  decommission-agent initiate-scan fetch-logs
  resolve-threat mark-threat-benign
  config metering raw
)

_s1am_metering_commands=(
  list-reports query summary
  workstation-endpoints server-endpoints container-hosts serverless-containers
  identity-endpoints identity-users
  xdr-ingested-bytes log-analytics-bytes
  hyperautomation-actions cns-workloads mobile-devices
)

_s1am_config_commands=(show list-profiles)

_s1am_list_opts="--limit --cursor --param --all --search --format --output-file --columns"
_s1am_output_opts="--format --output-file --columns"
_s1am_report_opts="--account-id --account-name --site-id --site-name --start-time --end-time --all-accounts --compare-previous --prev-start-time $_s1am_output_opts"

_s1am() {
  local cur prev words cword
  _init_completion || return

  # Determine command depth
  local cmd=""
  local sub=""
  local i
  for (( i=1; i<cword; i++ )); do
    case "${words[$i]}" in
      --*) ;;
      *)
        if [[ -z "$cmd" ]]; then
          cmd="${words[$i]}"
        elif [[ -z "$sub" ]]; then
          sub="${words[$i]}"
        fi
        ;;
    esac
  done

  case "$cmd" in
    "")
      if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_s1am_global_opts" -- "$cur"))
      else
        COMPREPLY=($(compgen -W "${_s1am_commands[*]}" -- "$cur"))
      fi
      ;;

    list-accounts|list-sites|list-agents|list-threats|list-groups|list-exclusions)
      COMPREPLY=($(compgen -W "$_s1am_list_opts" -- "$cur"))
      ;;

    get-account)
      COMPREPLY=($(compgen -W "$_s1am_output_opts" -- "$cur"))
      ;;

    account-policy)
      case "$prev" in
        account-policy) COMPREPLY=() ;;  # next arg is account_id
        *)
          if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "--payload --dry-run $_s1am_output_opts" -- "$cur"))
          else
            COMPREPLY=($(compgen -W "get update" -- "$cur"))
          fi
          ;;
      esac
      ;;

    update-site|create-site|move-agents)
      COMPREPLY=($(compgen -W "--payload --dry-run $_s1am_output_opts" -- "$cur"))
      ;;

    decommission-agent|initiate-scan|fetch-logs|resolve-threat|mark-threat-benign)
      COMPREPLY=($(compgen -W "--payload --dry-run $_s1am_output_opts" -- "$cur"))
      ;;

    raw)
      if [[ "$prev" == "raw" ]]; then
        COMPREPLY=($(compgen -W "GET POST PUT DELETE PATCH" -- "$cur"))
      else
        COMPREPLY=($(compgen -W "--param --payload --dry-run $_s1am_output_opts" -- "$cur"))
      fi
      ;;

    config)
      if [[ -z "$sub" ]]; then
        COMPREPLY=($(compgen -W "${_s1am_config_commands[*]}" -- "$cur"))
      else
        COMPREPLY=($(compgen -W "$_s1am_output_opts" -- "$cur"))
      fi
      ;;

    metering)
      if [[ -z "$sub" ]]; then
        COMPREPLY=($(compgen -W "${_s1am_metering_commands[*]}" -- "$cur"))
      else
        case "$sub" in
          list-reports)
            COMPREPLY=($(compgen -W "$_s1am_output_opts" -- "$cur"))
            ;;
          query)
            COMPREPLY=($(compgen -W "--start-time --end-time --all-pages $_s1am_output_opts" -- "$cur"))
            ;;
          summary)
            COMPREPLY=($(compgen -W "--account-id --start-time --end-time $_s1am_output_opts" -- "$cur"))
            ;;
          *)
            COMPREPLY=($(compgen -W "$_s1am_report_opts" -- "$cur"))
            ;;
        esac
      fi
      ;;

    --format)
      COMPREPLY=($(compgen -W "json table csv" -- "$cur"))
      ;;
  esac
}

complete -F _s1am s1am
complete -F _s1am python3 -m s1am 2>/dev/null
