import argparse
import bisect
import json
import os
import re


def ip_to_binary(ip_list):
    binary_list = []
    for ip in ip_list:
        octets, bits = ip.split('/')
        octets = octets.split('.')
        binary_ip = ''
        for octet in octets:
            binary_ip += bin(int(octet))[2:].zfill(8)
        binary_list.append(binary_ip[:int(bits)])
    return binary_list


def _get_cidr_ip(ptr):
    new_ip = ''
    while 'parent' in ptr:
        new_ip = ptr['bit'] + new_ip
        ptr = ptr['parent']
    if not new_ip:
        new_ip = '0'
    return new_ip


def _get_leafs(root):
    cidr_list = []
    nodes = [root]
    while nodes:
        node = nodes.pop()
        if 'ip' in node:
            cidr_list.append([node['ip'], node])
        else:
            if '0' in node:
                nodes.append(node['0'])
            if '1' in node:
                nodes.append(node['1'])
    return cidr_list


def get_cidrs(binary_ips, n=1):
    root = {}
    for ip in binary_ips:
        ptr = root
        for bit in ip:
            if bit not in ptr:
                ptr[bit] = {}
                ptr[bit]['parent'] = ptr
                ptr[bit]['bit'] = bit
            ptr = ptr[bit]
        ptr['ip'] = ip

    cidr_list = _get_leafs(root)
    cidr_list.sort(key=lambda x: [len(x[0]), x[0]])

    while len(cidr_list) > n:
        _, last_itm_node = cidr_list.pop()
        p_ptr = last_itm_node['parent']
        del p_ptr[last_itm_node['bit']]

        if 'ip' not in p_ptr:
            new_ip = _get_cidr_ip(p_ptr)
            p_ptr['ip'] = new_ip
            bisect.insort(cidr_list, [new_ip, p_ptr], key=lambda x: [len(x[0]), x[0]])

        leafs = []
        if '0' in p_ptr:
            leafs += _get_leafs(p_ptr['0'])
        if '1' in p_ptr:
            leafs += _get_leafs(p_ptr['1'])

        for leaf_ip, _ in leafs:
            cidr_list_pos = bisect.bisect_right(cidr_list, [len(leaf_ip), leaf_ip], key=lambda x: [len(x[0]), x[0]]) - 1
            del cidr_list[cidr_list_pos]

    cidr_list = [[x + ('0' * (32 - len(x))), len(x)] for x, _ in cidr_list]
    return ['.'.join([str(int(x[i:i+8], 2)) for i in range(0, 32, 8)]) + '/' + str(y) for x, y in cidr_list]


def parse_args():
    parser = argparse.ArgumentParser(
        description='Get CIDR ranges from GitHub'
    )
    parser.add_argument(
        '--rules-per-sg',
        help='Number of rules per security group',
        type=int,
        default=60,
    )
    parser.add_argument(
        '--output-file',
        help='Output file',
        type=str,
        default=os.path.join(os.path.dirname(__file__), '..', 'pet_instances', 'gh_sg.tf'),
    )
    options = parser.parse_args()
    return options


def main():
    options = parse_args()

    cidr_list = [
        # Meta
        "129.134.0.0/19",
        "66.220.144.0/20",
        "34.94.18.0/25",
        "35.192.199.128/25",
        "163.114.128.0/20",
        "157.240.128.0/18",
        "102.221.188.0/22",
        "31.13.96.0/19",
        "129.134.96.0/20",
        "18.190.96.139/32",
        "185.60.216.0/22",
        "102.132.112.0/20",
        "129.134.64.0/20",
        "157.240.192.0/18",
        "185.89.216.0/22",
        "35.239.7.131/32",
        "31.13.64.0/19",
        "204.15.20.0/22",
        "157.240.0.0/19",
        "179.60.192.0/22",
        "103.4.96.0/22",
        "69.63.176.0/20",
        "74.119.76.0/22",
        "163.70.128.0/17",
        "157.240.64.0/19",
        "69.171.224.0/19",
        "173.252.64.0/18",
        "129.134.80.0/20",
        "173.252.64.0/22",
        "147.75.208.0/20",
        "199.201.64.0/22",
        "66.111.48.0/22",
        "157.240.32.0/19",
        "163.77.128.0/17",
        "34.82.178.0/25",
        "129.134.32.0/19",
        "31.13.24.0/21",
        "45.64.40.0/22",
        "129.134.128.0/17",
        "102.132.96.0/20",
        # AWS
        "13.248.16.0/25",
        "13.248.48.0/25",
        "15.248.48.0/25",
        "15.248.54.236/31",
        "15.248.64.0/25",
        "15.248.70.236/31",
        "27.0.3.144/29",
        "27.0.3.152/29",
        "52.46.80.0/25",
        "52.46.208.0/25",
        "52.46.249.224/29",
        "52.46.249.248/29",
        "52.82.200.0/25",
        "52.94.36.0/25",
        "52.94.84.0/25",
        "52.94.133.128/25",
        "52.94.133.128/30",
        "52.94.133.136/30",
        "52.95.4.0/25",
        "52.95.75.0/25",
        "52.119.144.0/25",
        "54.222.61.32/28",
        "54.239.6.176/29",
        "54.239.6.184/29",
        "54.239.119.0/25",
        "54.240.193.0/29",
        "54.240.193.128/29",
        "54.240.196.160/27",
        "54.240.196.160/28",
        "54.240.196.176/28",
        "54.240.197.224/28",
        "54.240.198.32/29",
        "54.240.199.96/28",
        "54.240.217.8/29",
        "54.240.217.16/29",
        "69.157.200.212/32",
        "69.165.90.4/32",
        "69.165.90.12/32",
        "70.232.80.0/25",
        "70.232.112.0/25",
        "72.21.196.64/29",
        "72.21.198.64/29",
        "99.77.16.0/25",
        "99.77.48.0/25",
        "99.78.144.128/25",
        "99.78.200.0/25",
        "99.78.232.0/25",
        "99.82.144.0/25",
        "99.87.8.0/25",
        "104.153.113.16/28",
        "104.153.114.16/28",
        "177.72.241.16/29",
        "177.72.242.16/29",
        "204.246.162.32/28",
        "205.251.233.48/29",
        "205.251.233.104/29",
        "205.251.233.176/29",
        "205.251.233.232/29",
        "205.251.237.64/28",
        "205.251.237.96/28",
    ]

    ipv4 = ip_to_binary([x for x in cidr_list if re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', x)])
    ipv4 = get_cidrs(ipv4, options.rules_per_sg)

    with open(options.output_file, 'w') as f:
        f.write(f'''
# This file is generated by scripts/simplify_cidr_blocks.py
# DO NOT EDIT
locals {{
    # tflint-ignore: terraform_unused_declarations
    external_k8s_cidr_ipv4 = {json.dumps(ipv4)}
}}
''')

if __name__ == '__main__':
    main()
