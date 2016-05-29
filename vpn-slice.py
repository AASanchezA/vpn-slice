#!/usr/bin/env python3

from __future__ import print_function
from sys import stderr
import signal
import os, fcntl, time, subprocess as sp
import argparse
from collections import OrderedDict as odict
from ipaddress import ip_network, ip_address, IPv4Network, IPv6Network

DIG = '/usr/bin/dig'
IPROUTE = '/sbin/ip'
HOSTS = '/etc/hosts'

def networkify(host):
    try:
        return ip_network(host)
    except ValueError:
        return host

def names_for(host, domain, short=True, long=True):
    if '.' in host: first, rest = host.split('.', 1)
    else: first, rest = host, None

    names = []
    if long:
        if rest: names.append(host)
        elif domain: names.append(host+'.'+domain)
    if short:
        if not rest: names.append(host)
        elif rest==domain: names.append(first)
    return names

def write_hosts(host_map, tag):
    global HOSTS
    with open(HOSTS,'r+') as hostf:
        fcntl.flock(hostf, fcntl.LOCK_EX) # POSIX only, obviously
        lines = hostf.readlines()
        keeplines = [l for l in lines if not l.endswith('# %s\n'%tag)]
        hostf.seek(0,0)
        hostf.writelines(keeplines)
        for ip, names in host_map:
            print('%s %s\t\t# %s' % (ip, ' '.join(names), tag), file=hostf)
        hostf.truncate()
    return len(host_map) or len(lines)-len(keeplines)

def dig(host, dns, domain=None, reverse=False):
    global DIG
    host, dns = str(host), map(str, dns)
    cl = [DIG,'+short']+['@'+s for s in dns]+(['+domain='+domain] if domain else [])+(['-x'] if reverse else [])+[host]
    #print cl
    p = sp.Popen(cl, stdout=sp.PIPE)
    out = [l.strip() for l in p.communicate()[0].decode().splitlines()]
    if out and p.wait()==0:
        out = out[-1].rstrip('\n.')
        if reverse and out.split('.',1)[-1]==domain:
            out = out.split('.',1)[0]
        return out
    else:
        return None

def callip(*args):
    global IPROUTE
    cl = [IPROUTE]
    for arg in args:
        if isinstance(arg, dict):
            for k,v in arg.items():
                cl += [k] if v is None else [k, str(v)]
        else:
            cl.append(str(arg))

    if args[:2]==('route','get'): get, start, keys = 'route', 1, ('via','dev','src','mtu')
    elif args[:2]==('link','show'): get, start, keys = 'link', 3, ('mtu','state')
    else: get = None

    if get is None:
        sp.check_call(cl)
    else:
        w = sp.check_output(cl).decode().split()
        return {w[ii]:w[ii+1] for ii in range(start, len(w), 2) if w[ii] in keys}

# Environment variables which may be passed by our caller (as listed in /usr/share/vpnc-scripts/vpnc-script)
evs = ['reason', 'VPNGATEWAY', 'TUNDEV', 'CISCO_DEF_DOMAIN', 'CISCO_BANNER',
       'INTERNAL_IP4_ADDRESS', 'INTERNAL_IP4_MTU', 'INTERNAL_IP4_NETMASK', 'INTERNAL_IP4_NETMASKLEN', 'INTERNAL_IP4_NETADDR', 'INTERNAL_IP4_DNS', 'INTERNAL_IP4_NBNS',
       'INTERNAL_IP6_ADDRESS',                     'INTERNAL_IP6_NETMASK',                                                    'INTERNAL_IP6_DNS' ]
env = odict((k,os.environ[k]) for k in evs if k in os.environ)

reason = env.get('reason')
gateway = env.get('VPNGATEWAY')
tundev = env.get('TUNDEV')
domain = env.get('CISCO_DEF_DOMAIN')
banner = env.get('CISCO_BANNER')
myaddr = env.get('INTERNAL_IP4_ADDRESS')
mtu = env.get('INTERNAL_IP4_MTU')
netmask = env.get('INTERNAL_IP4_NETMASK')
netaddr = env.get('INTERNAL_IP4_NETADDR')
dns = [ip_address(x) for x in env.get('INTERNAL_IP4_DNS','').split()]
nbns = [ip_address(x) for x in env.get('INTERNAL_IP4_NBNS','').split()]

# Parse command-line arguments
p = argparse.ArgumentParser()
p.add_argument('hosts', nargs='*', type=networkify, help='List of VPN-internal hostnames or subnets to add to routing and /etc/hosts')
g = p.add_argument_group('Subprocess options')
p.add_argument('-k','--kill', default=[], action='append', help='File containing PID to kill before disconnect')
p.add_argument('--no-fork', action='store_false', dest='fork', help="Don't fork and continue in background on connect")
g = p.add_argument_group('Informational options')
g.add_argument('-v','--verbose', action='store_true', help="Show what I am doing during connect and disconnect")
g.add_argument('--banner', action='store_true', help='Pass banner message (default is to suppress it)')
g.add_argument('--dump', action='store_true', help='Dump environment variables passed by caller to vpnc-script')
g = p.add_argument_group('Routing and hostname options')
g.add_argument('-n','--name', default=tundev, help='Name of this VPN (default is $TUNDEV)')
g.add_argument('-d','--domain', default=domain, help='Search domain inside the VPN (default is $CISCO_DEF_DOMAIN)')
g.add_argument('--no-host-lookup', action='store_false', dest='host_lookup', default=True, help='Do not add either short or long hostnames to /etc/hosts')
g.add_argument('--no-short-names', action='store_false', dest='short_names', default=True, help="Only add long/fully-qualified domain names to /etc/hosts")
g.add_argument('--no-ns-lookup', action='store_false', dest='ns_lookup', default=True, help='Do not lookup nameservers and add them to /etc/hosts')
args = p.parse_args()

subnets = [x for x in args.hosts if isinstance(x, (IPv4Network, IPv6Network))]
hosts = [x for x in args.hosts if not isinstance(x, (IPv4Network, IPv6Network))]
if reason is None:
    p.error("Must be called as vpnc-script, with $reason set")

if args.dump:
    ppid = os.getppid()
    try:
        caller = '%s (PID %d)' % (os.readlink('/proc/%d/exe'%ppid), ppid)
    except OSError:
        caller = 'PID %d' % parent

    print('Called by %s with environment variables for vpnc-script:' % caller, file=stderr)
    for var,val in env.items():
        print('  %s=%s' % (var, repr(val)), file=stderr)

########################################

if reason=='pre-init':
    if not os.access('/dev/net/tun', os.R_OK|os.W_OK):
        raise OSError("can't read and write /dev/net/tun")

elif reason=='disconnect':
    for pidfile in args.kill:
        try:
            pid = int(open(pidfile).read())
            os.kill(pid, signal.SIGTERM)
            if args.verbose:
                print("Killed pid %d from %s" % (pid, pidfile), file=stderr)
        except (IOError, ValueError, OSError):
            pass

    removed = write_hosts({}, 'vpn-slice-%s AUTOCREATED' % args.name)
    if args.verbose:
        print("Removed %d hosts from /etc/hosts" % removed, file=stderr)

    # delete explicit route to gateway
    try:
        callip('route', 'del', gateway)
    except sp.CalledProcessError:
        print("WARNING: could not delete route to VPN gateway (%s)" % gateway, file=stderr)

elif reason=='connect':
    if args.banner and banner:
        print("Connect Banner:")
        for l in banner.splitlines(): print("| "+l)

    # set explicit route to gateway
    gwr = callip('route', 'get', gateway)
    callip('route', 'replace', gateway, gwr)

    # configure MTU
    if mtu is None:
        dev = gwr.get('dev')
        mtudev = dev and callip('link', 'show', dev).get('mtu')
        mtu = mtudev and int(mtudev) - 88
        if mtu:
            print("WARNING: did not get INTERNAL_IP4_MTU, guessing %d (MTU of %s - 88)" % (mtu, dev), file=stderr)
        else:
            mtu = 1412
            print("WARNING: did not get INTERNAL_IP4_MTU, default guess is %d" % mtu, file=stderr)
    callip('link', 'set', 'dev', tundev, 'up', 'mtu', mtu)

    # set peer
    callip('addr', 'add', IPv4Network(myaddr), 'peer', myaddr, 'dev', tundev)

    # set up routes to the DNS and Windows name servers
    for dest in dns+nbns+subnets:
        callip('route', 'replace', dest, 'dev', tundev)
    else:
        callip('route', 'flush', 'cache')
        if args.verbose:
            print("Added routes for %d nameservers and %d subnets." % (len(dns)+len(nbns), len(subnets)), file=stderr)

    # we continue running in a new child process, so the VPN can actually
    # start in the background, because we need to actually send traffic to it
    if args.fork and os.fork():
        raise SystemExit

    # lookup named hosts for which we need routes and/or host_map entries
    # (the DNS/NBNS servers already have their routes)
    ip_routes = set()
    host_map = []

    if args.ns_lookup:
        if args.verbose:
            print("Doing reverse lookup for %d nameservers..." % (len(dns)+len(nbns)), file=stderr)
        for ip in dns+nbns:
            host = dig(ip, dns, args.domain, reverse=True)
            if host is None:
                print("WARNING: Reverse lookup for %s on VPN DNS servers (%s) failed." % (ip, ', '.join(map(str,dns))), file=stderr)
            else:
                names = names_for(host, args.domain, args.short_names)
                if args.verbose:
                    print("  %s = %s" % (ip, ', '.join(names)))
                host_map.append((ip, names))

    if args.verbose:
        print("Looking up %d hosts using VPN DNS servers..." % len(args.hosts), file=stderr)
    for host in hosts:
        ip = dig(host, dns, args.domain)
        if ip is None:
            print("WARNING: Lookup for %s on VPN DNS servers (%s) failed." % (host, ', '.join(map(str,dns))), file=stderr)
        else:
            if args.verbose:
                print("  %s = %s" % (host, ip), file=stderr)
            ip_routes.add(ip)
            if args.host_lookup:
                names = names_for(host, args.domain, args.short_names)
                host_map.append((ip, names))

    # add them to /etc/hosts
    if host_map:
        write_hosts(host_map, 'vpn-slice-%s AUTOCREATED' % args.name)
        if args.verbose:
            print("Added %d VPN hosts to /etc/hosts." % len(host_map), file=stderr)

    # add routes to hosts
    for ip in ip_routes:
        callip('route', 'replace', ip, 'dev', tundev)
    else:
        callip('route', 'flush', 'cache')
        if args.verbose:
            print("Added routes for %d named hosts." % len(ip_routes), file=stderr)