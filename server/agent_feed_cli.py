"""`tether graph` connects the work board feeds to Microsoft 365 with your own Entra app; `tether feed` shows the
feeds.

graph connect signs in with the device code flow: it prints a code and a Microsoft page, you sign in there, and Tether
saves the connection (a refresh token) in a private file, mode 600. No token is ever on the command line or in the
board. The Entra app needs these delegated permissions: offline_access and User.Read; Mail.ReadBasic for an email
feed; Team.ReadBasic.All, Channel.ReadBasic.All and ChannelMessage.Read.All (admin consent) for a Teams feed. Turn on
"Allow public client flows" in the app's Authentication page."""
import argparse
import sys
import time

import graph_client
import work_feeds
import work_items


def _kinds(text):
    kinds = {k.strip() for k in (text or '').split(',') if k.strip()}
    if not kinds or kinds - {'mail', 'teams'}:
        raise ValueError('--feeds takes mail, teams, or mail,teams.')
    return kinds


def connect(args, open_url=None, sleep=time.sleep):
    try:
        tenant, client = graph_client.check(args.tenant, args.client)
        scopes = graph_client.scopes_for(_kinds(args.feeds))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        start = graph_client.device_start(tenant, client, scopes, open_url=open_url)
        code, user_code = start.get('device_code'), start.get('user_code')
        if not isinstance(code, str) or not isinstance(user_code, str):
            print('Microsoft gave no sign-in code. Nothing saved.', file=sys.stderr)
            return 1
        print(f'Open {start.get("verification_uri") or "https://microsoft.com/devicelogin"} and enter the code '
              f'{user_code}. Sign in with the account whose Teams and mail the feeds read.', flush=True)
        answer = graph_client.device_finish(tenant, client, code, int(start.get('interval') or 5),
                                            int(start.get('expires_in') or 900), open_url=open_url, sleep=sleep)
    except graph_client.GraphError as exc:
        print(f'{exc} Nothing saved.', file=sys.stderr)
        return 1
    refresh, access = answer.get('refresh_token'), answer.get('access_token')
    if not isinstance(refresh, str) or not refresh or not isinstance(access, str) or not access:
        print('Microsoft gave no refresh token (is offline_access allowed?). Nothing saved.', file=sys.stderr)
        return 1
    granted = str(answer.get('scope') or '').split()
    config = {'tenant': tenant, 'client': client, 'refresh': refresh, 'scopes': scopes, 'granted': granted}
    graph_client.save(config)
    graph_client._ACCESS.clear()
    graph_client._ACCESS[(tenant, client, refresh)] = (access, time.time() + max(60, int(answer.get('expires_in') or 3600)))
    try:
        me = graph_client.Client(config, open_url=open_url, deadline=30).get('/me?$select=displayName,userPrincipalName')
    except graph_client.GraphError as exc:
        graph_client.remove()
        print(f'Microsoft did not show the account ({exc}). Nothing saved.', file=sys.stderr)
        return 1
    if not graph_client.update({'account': str(me.get('userPrincipalName') or '')[:256],
                                'name': str(me.get('displayName') or '')[:128]}):
        print('The connection was removed while it was being saved. Run tether graph connect again.', file=sys.stderr)
        return 1
    return show(args)


def show(_args):
    shown = graph_client.status()
    if not shown['connected']:
        print(shown['why'])
        return 1
    print(f'Connected to Microsoft 365 as {shown["account"] or "your account"}.')
    print('Email feeds: ' + ('on' if shown['mail'] else 'off (no Mail.ReadBasic)'))
    print('Teams feeds: ' + ('on' if shown['teams'] else 'off (no ChannelMessage.Read.All)'))
    return 0


def disconnect(_args):
    graph_client.remove()
    print('Microsoft 365 disconnected. Teams and email feeds stop.')
    return 0


def graph_main(argv=None):
    parser = argparse.ArgumentParser(prog='tether graph', description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    made = commands.add_parser('connect', help='sign in with a code on a Microsoft page')
    made.add_argument('--tenant', required=True, help='the directory (tenant) id of your Entra app, or your domain')
    made.add_argument('--client', required=True, help='the application (client) id of your Entra app')
    made.add_argument('--feeds', default='mail,teams',
                      help='mail, teams, or mail,teams (default). Teams needs admin consent for ChannelMessage.Read.All')
    made.set_defaults(run=connect)
    commands.add_parser('status', help='show the connection').set_defaults(run=show)
    commands.add_parser('disconnect', help='remove the saved sign-in').set_defaults(run=disconnect)
    args = parser.parse_args(argv)
    return args.run(args)


def feed_status(args):
    shown = work_feeds.statuses()
    projects = {p['name']: p for p in work_items.registry()['projects']}
    names = [args.project] if args.project else sorted(projects)
    for name in names:
        record = projects.get(name)
        if record is None:
            print(f'No project named {name}.', file=sys.stderr)
            return 2
        state = shown.get(name)
        if not state:
            print(f'{name}: by hand')
            continue
        feed = f'{record["feed"]} {record.get("feedTarget") or ""}'.strip()
        if state['by'] == 'agent':
            print(f'{name}: {feed}, read by the project\'s own agent')
            continue
        when = time.strftime('%H:%M', time.localtime(state['at'])) if state.get('at') else 'not yet'
        line = f'{name}: {feed}, {state["status"]} (last read {when}, {state["items"]} items)'
        print(line + (f': {state["error"]}' if state.get('error') else ''))
    return 0


def feed_main(argv=None):
    parser = argparse.ArgumentParser(prog='tether feed', description='Show the work board feeds.')
    commands = parser.add_subparsers(dest='command', required=True)
    made = commands.add_parser('status', help='each project\'s feed and its last read')
    made.add_argument('project', nargs='?')
    made.set_defaults(run=feed_status)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == '__main__':
    sys.exit(graph_main())
