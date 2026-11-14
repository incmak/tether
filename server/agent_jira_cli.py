"""`tether jira`: connect the work board to Jira with Mak's own API token, so Done moves the item's tickets.

connect reads the token from a hidden prompt (or one line on stdin), never from the command line. It checks the token
with /myself and saves the connection only when Jira accepts it. Create a token at
https://id.atlassian.com/manage-profile/security/api-tokens (scopes: read:jira-work, write:jira-work,
read:jira-user)."""
import argparse
import getpass
import sys

import jira_client


def connect(args):
    try:
        base = jira_client.check_base(args.site)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    email = (args.email or '').strip()
    if '@' not in email:
        print('Give the email of your Atlassian account with --email.', file=sys.stderr)
        return 2
    token = getpass.getpass('Jira API token: ') if sys.stdin.isatty() else sys.stdin.readline()
    token = token.strip()
    if not token:
        print('No token given. Nothing saved.', file=sys.stderr)
        return 2
    config = {'base': base, 'email': email, 'token': token, 'guard': bool(args.assignee_guard)}
    try:
        me = jira_client.Client(config).myself()
    except jira_client.JiraError as exc:
        text = 'Jira refused the token or the email.' if exc.status in (401, 403) else str(exc)
        print(f'{text} Nothing saved.', file=sys.stderr)
        return 1
    if not isinstance(me.get('accountId'), str) or not me['accountId']:
        print('Jira gave no account id for this token. Nothing saved.', file=sys.stderr)
        return 1
    jira_client.save({**config, 'accountId': me['accountId'], 'name': str(me.get('displayName') or '')[:128]})
    shown = jira_client.status()
    print(f'Connected to {shown["site"]} as {shown["account"]}.')
    print('Done items move the tickets assigned to you.' if shown['guard'] else shown['why'])
    return 0


def show(_args):
    shown = jira_client.status()
    if shown['connected']:
        print(f'Connected to {shown["site"]} as {shown["account"]}.')
        print('Done items move the tickets assigned to you.' if shown['guard'] else shown['why'])
        return 0
    print(shown['why'])
    return 1


def disconnect(_args):
    jira_client.remove()
    print('Jira disconnected. Done items no longer move tickets.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tether jira', description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    made = commands.add_parser('connect', help='connect with your API token (asked for, never on the command line)')
    made.add_argument('--site', required=True,
                      help='https://<site>.atlassian.net, or https://api.atlassian.com/ex/jira/<cloud id> for a token with scopes')
    made.add_argument('--email', required=True, help='the email of your Atlassian account')
    made.add_argument('--assignee-guard', action='store_true',
                      help='the Jira workflow has the Only Assignee condition on the Done and QA transitions: only '
                           'then does Tether move tickets, so a ticket given to someone else in the meantime never moves')
    made.set_defaults(run=connect)
    commands.add_parser('status', help='show the connection').set_defaults(run=show)
    commands.add_parser('disconnect', help='remove the saved token').set_defaults(run=disconnect)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == '__main__':
    sys.exit(main())
