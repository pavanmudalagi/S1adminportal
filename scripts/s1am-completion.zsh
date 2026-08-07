#compdef s1am
# zsh completion for s1am
# Install:
#   cp s1am-completion.zsh /usr/local/share/zsh/site-functions/_s1am
# or add to your ~/.zshrc:
#   source /path/to/s1am-completion.zsh

_s1am_formats=(json table csv)
_s1am_methods=(GET POST PUT DELETE PATCH)

_s1am_global_flags=(
  '--version[Show version and exit]'
  '--profile[Config profile name]:profile'
  '--verbose[Print HTTP debug info to stderr]'
  '--quiet[Suppress progress/warning output]'
)

_s1am_output_flags=(
  '--format[Output format]:format:((json\:"JSON (default)" table\:"ASCII table" csv\:"CSV"))'
  '--output-file[Write output to a file]:file:_files'
  '--columns[Comma-separated column list]:columns'
)

_s1am_list_flags=(
  "${_s1am_output_flags[@]}"
  '--limit[Max items per page]:limit'
  '--cursor[Pagination cursor]:cursor'
  '--param[Extra query param key=value]:param'
  '--all[Fetch all pages automatically]'
  '--search[Filter by name]:search'
)

_s1am_report_flags=(
  "${_s1am_output_flags[@]}"
  '--account-id[Account ID]:id'
  '--account-name[Filter by account name]:name'
  '--site-id[Site ID]:id'
  '--site-name[Site name]:name'
  '--start-time[Time range start (e.g. 31d)]:time'
  '--end-time[Time range end]:time'
  '--all-accounts[Run for all accounts]'
  '--compare-previous[Compare current vs previous period]'
  '--prev-start-time[Previous period start time]:time'
)

_s1am_action_flags=(
  "${_s1am_output_flags[@]}"
  '--payload[Override JSON payload]:json'
  '--dry-run[Preview without executing]'
)

_s1am() {
  local context state state_descr line
  typeset -A opt_args

  _arguments -C \
    "${_s1am_global_flags[@]}" \
    '1:command:->command' \
    '*::args:->args'

  case $state in
    command)
      local commands=(
        'list-accounts:List accounts'
        'get-account:Get account by ID'
        'account-policy:Get or update account policy'
        'list-sites:List sites'
        'list-agents:List agents/endpoints'
        'list-threats:List threats'
        'list-groups:List agent groups'
        'list-exclusions:List exclusions'
        'update-site:Update a site'
        'create-site:Create a new site'
        'move-agents:Move agents to a different site'
        'decommission-agent:Decommission an agent'
        'initiate-scan:Initiate a full disk scan'
        'fetch-logs:Fetch logs from an agent'
        'resolve-threat:Mark a threat as resolved'
        'mark-threat-benign:Mark a threat as benign'
        'config:Manage configuration and profiles'
        'metering:Usage Metering Platform commands'
        'raw:Call any API endpoint directly'
      )
      _describe 'command' commands
      ;;

    args)
      case $line[1] in
        list-accounts|list-sites|list-agents|list-threats|list-groups|list-exclusions)
          _arguments "${_s1am_list_flags[@]}"
          ;;
        get-account)
          _arguments "${_s1am_output_flags[@]}" '1:account_id'
          ;;
        account-policy)
          _arguments "${_s1am_output_flags[@]}" \
            '1:account_id' \
            '2:action:((get update))' \
            '--payload[JSON payload for update]:json' \
            '--dry-run[Preview without executing]'
          ;;
        update-site)
          _arguments "${_s1am_action_flags[@]}" '1:site_id'
          ;;
        create-site|move-agents)
          _arguments "${_s1am_action_flags[@]}"
          ;;
        decommission-agent|initiate-scan|fetch-logs)
          _arguments "${_s1am_action_flags[@]}" '1:agent_id'
          ;;
        resolve-threat|mark-threat-benign)
          _arguments "${_s1am_action_flags[@]}" '1:threat_id'
          ;;
        raw)
          _arguments "${_s1am_output_flags[@]}" \
            '1:method:((GET POST PUT DELETE PATCH))' \
            '2:path' \
            '--param[Query param key=value]:param' \
            '--payload[JSON payload]:json' \
            '--dry-run[Preview without executing]'
          ;;
        config)
          local config_cmds=(
            'show:Show resolved config (tokens masked)'
            'list-profiles:List available profiles'
          )
          _arguments '1:subcommand:->sub'
          case $state in
            sub) _describe 'config subcommand' config_cmds ;;
          esac
          ;;
        metering)
          local metering_cmds=(
            'list-reports:List available metering reports'
            'query:Execute raw PowerQuery DSL'
            'summary:All billing reports for an account'
            'workstation-endpoints:EDR workstation counts'
            'server-endpoints:CWS server counts'
            'container-hosts:CWS container host counts'
            'serverless-containers:CWS serverless counts'
            'identity-endpoints:Identity IDR counts'
            'identity-users:Identity user counts'
            'xdr-ingested-bytes:SDL ingested bytes'
            'log-analytics-bytes:Log Analytics bytes'
            'hyperautomation-actions:Hyperautomation action counts'
            'cns-workloads:Cloud Native Security workloads'
            'mobile-devices:Mobile device counts'
          )
          _arguments '1:subcommand:->sub' '*::sub_args:->sub_args'
          case $state in
            sub) _describe 'metering subcommand' metering_cmds ;;
            sub_args)
              case $line[1] in
                list-reports) _arguments "${_s1am_output_flags[@]}" ;;
                query)
                  _arguments "${_s1am_output_flags[@]}" \
                    '1:query_string' \
                    '--start-time:time' '--end-time:time' '--all-pages'
                  ;;
                summary)
                  _arguments "${_s1am_output_flags[@]}" \
                    '--account-id[Account ID]:id' \
                    '--start-time:time' '--end-time:time'
                  ;;
                *) _arguments "${_s1am_report_flags[@]}" ;;
              esac
              ;;
          esac
          ;;
      esac
      ;;
  esac
}

_s1am "$@"
