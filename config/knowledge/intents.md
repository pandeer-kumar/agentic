# Intent mappings

Each `##` section is one business intent. The agent reads this file through
`resolve_intent`, so adding a section needs no code change.

Fields:
- `aliases`: phrases users say, comma separated
- `systems`: systems that answer this intent
- `boards`: Jira board keys
- `fetch`: `system.tool {json arguments}`. `{hours}` is filled from the user's time window
- `group_by`: useful breakdown columns
- `notes`: guidance for the agent

## network changes
- aliases: network change, network changes, firewall change, router change, switch change, bgp, dns change, load balancer change
- systems: jira
- boards: NET, NOC
- fetch: jira.search_issues {"jql": "project in (NET, NOC) AND issuetype = Change AND updated >= -{hours}h"}
- group_by: status, priority, assignee_team
- notes: A network change is a Jira issue of type Change on board NET or NOC.

## database changes
- aliases: database change, database changes, db change, schema change, migration
- systems: jira
- boards: DBA
- fetch: jira.search_issues {"jql": "project in (DBA) AND issuetype = Change AND updated >= -{hours}h"}
- group_by: status, priority

## application releases
- aliases: release, releases, deployment, deployments, deploy, app change
- systems: jira, github
- boards: APP
- fetch: jira.search_issues {"jql": "project in (APP) AND issuetype = Change AND updated >= -{hours}h"}
- fetch: github.list_events {"hours": {hours}}
- group_by: status, service

## customer issues
- aliases: customer issue, customer issues, user issue, users facing, issues users, what issues, complaints, support tickets, zendesk issues, kind of issues, facing
- systems: zendesk
- fetch: zendesk.search_tickets {"hours": {hours}}
- group_by: category, priority, status
- notes: Group by category, then read subjects and descriptions to explain the themes.

## on-call alerts
- aliases: alert, alerts, page, pages, paging, on-call, oncall, escalation, escalations, xmatters
- systems: xmatters, pagerduty
- fetch: xmatters.list_events {"hours": {hours}}
- fetch: pagerduty.list_events {"hours": {hours}}
- group_by: severity, status, service

## incidents
- aliases: incident, incidents, outage, outages, major incident, sev1, p1
- systems: jira, servicenow, pagerduty, statuspage
- boards: INC
- fetch: jira.search_issues {"jql": "project in (INC) AND updated >= -{hours}h"}
- fetch: servicenow.list_events {"hours": {hours}}
- fetch: pagerduty.list_events {"hours": {hours}}
- fetch: statuspage.list_events {"hours": {hours}}
- group_by: severity, status, service

## monitoring health
- aliases: monitor, monitors, metrics, infrastructure health, datadog
- systems: datadog
- fetch: datadog.list_events {"hours": {hours}}
- group_by: severity, service

## documentation
- aliases: runbook, runbooks, postmortem, postmortems, documentation, confluence
- systems: confluence
- fetch: confluence.list_events {"hours": {hours}}
- group_by: service

## team discussion
- aliases: slack, discussion, chatter, channel messages
- systems: slack
- fetch: slack.list_events {"hours": {hours}}
- group_by: service
