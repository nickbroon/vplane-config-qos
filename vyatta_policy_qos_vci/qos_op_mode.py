#!/usr/bin/env python3
#
# Copyright (c) 2019, AT&T Intellectual Property.
# All rights reserved
#
# SPDX-License-Identifier: LGPL-2.1-only
#
"""
The QoS VCI operation-mode module.  Responsible for converting JSON containing
the QoS operational-mode state, as returned by the vyatta-dataplane in
response to the "qos optimised-show" vplsh command, into Yang compatible JSON.

The caller of this script issues a "qos optimised-show" command to the vyatta
dataplane and in return receives the QoS operational state (including counters)
encoded in JSON.  Unfortunately this JSON encoding is not compatible with Yang
as it uses untagged array elements, where the array index is implicitly used as
the tag.  Yang lists (which is the nearest thing to a JSON untagged array)
can't cope with this, so this script converts the received JSON (which is
significantly more compact than Yang compatible JSON) into Yang compatible JSON.

The entrypoint to this module is the function convert_if_list.  The caller
of convert_if_list passes in non-Yang compatiable JSON dictionary, and is
returned a Yang compatible JSON dictionary.   convert_if_list does this
conversion by descending down the multiple levels of the original JSON
dictionary converting non-Yang compatible JSON arrays into Yang compatible
lists (the major part of the conversation).

There is also a "disconnect" between the vyatta QoS configuration model
(interface names, vlan names, QoS policies and profiles), and the underlying
Intel DPDK QoS framework (ports, subport, pipes, traffic-classes, and queues).
The JSON returned by the vyatta-dataplane is based upon the DPDK QoS
framework, so this script maps some of the underlying DPDK entities onto the
appropriate QoS configuration model entities that customers might have some
chance of understanding.

This module was translated from VR/vplane-config-qos/scripts/qos-op-mode.pl
"""

import re

from vyatta_policy_qos_vci.provisioner import get_existing_config

TC_SHIFT = 2
TC_MASK = 0x3
WRR_MASK = 0x7

config = {}

def get_sysfs_value(ifname, valuename):
    """
    Return the value of a Linux sysfs interface attribute or an empty string
    if the sysfs file does not exist.
    """
    value = ""
    filename = "/sys/class/net/{}/{}".format(ifname, valuename)
    try:
        with open(filename, 'r') as data:
            value = data.read()
    except OSError:
        # For unit-testing return a fixed if-index of 8
        return "8"

    return value.strip()


def get_port_policy_name(if_type_dict):
    """
    Return the name of the QoS policy attached to a trunk interface or
    None if no QoS policy is attached.
    """
    # Physical ports
    try:
        # Standard VM policy attachment point
        policy_dict = if_type_dict['vyatta-interfaces-policy-v1:policy']
    except KeyError:
        try:
            # Hardware switch policy attachment point
            policy_dict = if_type_dict['vyatta-interfaces-dataplane-switch-v1:switch-group']
            port_params_dict = policy_dict['port-parameters']
            policy_dict = port_params_dict['vyatta-interfaces-switch-policy-v1:policy']
        except KeyError:
            return None

    return policy_dict['vyatta-policy-qos-v1:qos']


def get_vlan_policy_name(if_type_dict, vlan_tag):
    """
    Return the name of the QoS policy attached to a vlan interface or
    None if no QoS policy is attached.
    """
    # Vlan ports
    vif_list = if_type_dict.get('vif')
    if vif_list is not None:
        # Standard VM vlan policy attachment point
        for vif_dict in vif_list:
            if vif_dict['tagnode'] == vlan_tag:
                policy_dict = vif_dict['vyatta-interfaces-policy-v1:policy']
                return policy_dict['vyatta-policy-qos-v1:qos']
    else:
        # Hardware switch vlan policy attachment point
        try:
            policy_dict = if_type_dict['vyatta-interfaces-dataplane-switch-v1:switch-group']
            port_params_dict = policy_dict['port-parameters']
            qos_params_dict = port_params_dict['qos-parameters']
            vlan_list = qos_params_dict['vlan']
            for vlan_dict in vlan_list:
                if vlan_dict['vlan-id'] == vlan_tag:
                    policy_dict = port_params_dict['vyatta-interfaces-switch-policy-v1:policy']
                    return policy_dict['vyatta-policy-qos-v1:qos']
        except KeyError:
            return None

    return None


def get_if_subport_policy_name(subport_name):
    """
    Return the policy name attached to this specified subport name.
    The subport name is in the form "<if-name>[ vif <vlan-tag>]".
    """
    global config

    index = subport_name.find(' vif ')
    if index == -1:
        if_name = subport_name
        vlan_tag = None
    else:
        if_name = subport_name[:index]
        vlan_tag = subport_name[index+5:]

    if config == {}:
        config = get_existing_config()

    if_types_dict = config.get('vyatta-interfaces-v1:interfaces')
    if if_types_dict is not None:
        for if_list in if_types_dict.values():
            for if_dict in if_list:
                if if_dict['tagnode'] == if_name:
                    if vlan_tag is None:
                        return get_port_policy_name(if_dict)

                    return get_vlan_policy_name(if_dict, vlan_tag)

    return None


def get_policy_class_profile_name(policy_name, pipe_id):
    """
    Return the profile name for the policy/pipe combination.
    pipe_id = 0 for the default profile, 1-255 for class profiles.
    May return None.
    """
    global config

    if config == {}:
        config = get_existing_config()

    policy_dict = config.get('vyatta-policy-v1:policy')
    if policy_dict is not None:
        qos_policy_dict = policy_dict.get('vyatta-policy-qos-v1:qos')
        policy_name_list = qos_policy_dict['name']
        for policy in policy_name_list:
            if policy['id'] == policy_name:
                shaper_dict = policy['shaper']
                if pipe_id == 0:
                    return shaper_dict.get('default')

                class_list = shaper_dict.get('class')
                if class_list is not None:
                    for class_dict in class_list:
                        if class_dict['id'] == pipe_id:
                            return class_dict.get('profile')

    return None


def get_traffic_class(qmap_value):
    """ Extract the traffic-class from a qmap value """
    return qmap_value & TC_MASK


def get_queue_number(qmap_value):
    """ Extract the wrr-id from a qmap value """
    return (qmap_value >> TC_SHIFT) & WRR_MASK


def convert_tc_rates(tc_rates_in):
    """
    Convert a 'tc_rates' JSON array into Yang compatible 'tagged' JSON array,
    tagged by traffic-class-id (0..3)
    """
    tc_rates_out = []
    tc_id = 0

    for tc_rate in tc_rates_in:
        tc_rate_out = {}

        tc_rate_out['traffic-class'] = tc_id
        tc_rate_out['rate'] = tc_rate
        tc_rates_out.append(tc_rate_out)
        tc_id += 1

    return tc_rates_out


def convert_wrr_weights(wrr_weights_in):
    """
    Convert a 'wrr_weights' JSON array into Yang compatible 'tagged' JSON array
    tagged by wrr-queue-id (0..7)
    """
    wrr_weights_out = []
    queue_id = 0

    for wrr in wrr_weights_in:
        wrr_weight_out = {}

        wrr_weight_out['queue'] = queue_id
        wrr_weight_out['weight'] = wrr
        wrr_weights_out.append(wrr_weight_out)
        queue_id += 1

    return wrr_weights_out


def convert_dscp_map(dscp_map_in):
    """
    Convert a 'dscp2q' JSON array into Yang compatible 'tagged' JSON array,
    tagged by dscp-value (0..63).
    Also build a tc-id/wrr-id to dscp mapping.
    """
    dscp_map_out = []
    tc_queue_to_dscp_map = {}
    dscp_id = 0

    if dscp_map_in is not None:
        for dscp_map_value in dscp_map_in:
            dscp_out = {}
            dscp_values = []
            tc_id = get_traffic_class(dscp_map_value)
            q_id = get_queue_number(dscp_map_value)

            dscp_out['dscp'] = dscp_id
            # The following two elements are defined in
            # vyatta-policy-qos-groupings-v1.yang hence the need
            # to include their namespace.
            dscp_out['vyatta-policy-qos-groupings-v1:traffic-class'] = tc_id
            dscp_out['vyatta-policy-qos-groupings-v1:queue'] = q_id
            dscp_map_out.append(dscp_out)

            try:
                dscp_values = tc_queue_to_dscp_map[tc_id][q_id]

            except KeyError:
                dscp_values = []

            dscp_values.append(dscp_id)

            try:
                tc_queue_to_dscp_map[tc_id][q_id] = dscp_values

            except KeyError:
                try:
                    tc_queue_to_dscp_map[tc_id] = {}
                    tc_queue_to_dscp_map[tc_id][q_id] = dscp_values

                except KeyError:
                    pass

            dscp_id += 1

    return (dscp_map_out, tc_queue_to_dscp_map)


def convert_pcp_map(pcp_map_in):
    """
    Convert a 'pcp2q' JSON array into Yang compatible 'tagged' JSON array,
    tagged by pcp-value (0..7).
    Also build a tc-id/wrr-id to pcp mapping.
    """
    pcp_map_out = []
    tc_queue_to_pcp_map = []
    pcp_id = 0

    if pcp_map_in is not None:
        for pcp_value in pcp_map_in:
            pcp_out = {}
            pcp_values = []
            tc_id = get_traffic_class(pcp_value)
            q_id = get_queue_number(pcp_value)

            pcp_out['pcp'] = pcp_id
            # The following two elements are defined in
            # vyatta-policy-qos-groupings-v1.yang hence the need
            # to include their namespace.
            pcp_out['vyatta-policy-qos-groupings-v1:traffic-class'] = tc_id
            pcp_out['vyatta-policy-qos-groupings-v1:queue'] = q_id
            pcp_map_out.append(pcp_out)

            if tc_queue_to_pcp_map[tc_id][q_id] is not None:
                pcp_values = tc_queue_to_pcp_map[tc_id][q_id]
            else:
                pcp_values = []

                pcp_values.append(pcp_id)
                tc_queue_to_pcp_map[tc_id][q_id] = pcp_values

                pcp_id += 1

    return (pcp_map_out, tc_queue_to_pcp_map)


def convert_map_list(map_list, map_type):
    """ Convert either a dscp or pcp reversed map into Yang JSON format """
    map_list_out = []

    for value in map_list:
        value_out = {}

        value_out[map_type] = value
        map_list_out.append(value_out)

    return map_list_out


def convert_wred_map_list(map_list_in):
    """
    Convert a 'wred_map' JSON array into Yang compatible 'tagged' JSON array,
    tagged by the resource-group name
    """
    map_list_out = []

    for map_in in map_list_in:
        map_out = {}

        map_out['res-grp'] = map_in['res_grp']
        map_out['random-dscp-drop'] = map_in['random_dscp_drop'] & 0xffffffff
        map_list_out.append(map_out)

    return map_list_out


def convert_wred_map_list_64(map_list_in):
    """
    Convert a 'wred_map' JSON array into Yang compatible 'tagged' JSON array,
    tagged by the resource-group name
    """
    map_list_out = []

    for map_in in map_list_in:
        map_out = {}

        map_out['res-grp-64'] = map_in['res_grp']
        map_out['random-dscp-drop-64'] = map_in['random_dscp_drop']
        map_list_out.append(map_out)

    return map_list_out


def convert_tc_queues(tc_queues_in, tc_id, reverse_map, map_type_values):
    """
    Convert a list of traffic-class 'queue' JSON dictionaries into Yang
    compatible 'tagged' JSON array, tagged by wrr-queue-id (0..7)
    """
    tc_queues_out = []
    queue_id = 0

    for queue in tc_queues_in:
        queue_out = {}

        queue_out['queue'] = queue_id

        # The following elements are defined in
        # vyatta-policy-qos-groupings-v1.yang hence the need to include their
        # namespace.
        # Truncate the value for the old 32-bit counters
        queue_out['vyatta-policy-qos-groupings-v1:packets'] = (
            queue['packets'] & 0xffffffff)
        queue_out['vyatta-policy-qos-groupings-v1:bytes'] = (
            queue['bytes'] & 0xffffffff)

        # The dropped counter is total drops, we just want tail-drops
        queue_out['vyatta-policy-qos-groupings-v1:dropped'] = ((
            queue['dropped'] - queue['random_drop']) & 0xffffffff)
        queue_out['vyatta-policy-qos-groupings-v1:random-drop'] = (
            queue['random_drop'] & 0xffffffff)
        if queue.get('wred_map') is not None:
            queue_out['vyatta-policy-qos-groupings-v1:wred-map'] = (
                convert_wred_map_list(queue['wred_map']))

        # Don't truncate the new 64-bit counters
        queue_out['vyatta-policy-qos-groupings-v1:packets-64'] = queue['packets']
        queue_out['vyatta-policy-qos-groupings-v1:bytes-64'] = queue['bytes']

        # The dropped counter is total drops, we just want tail-drops
        queue_out['vyatta-policy-qos-groupings-v1:dropped-64'] = (
            queue['dropped'] - queue['random_drop'])
        queue_out['vyatta-policy-qos-groupings-v1:random-drop-64'] = (
            queue['random_drop'])
        if queue.get('wred_map') is not None:
            queue_out['vyatta-policy-qos-groupings-v1:wred-map-64'] = (
                convert_wred_map_list_64(queue['wred_map']))

        if queue.get('qlen') is not None:
            queue_out['vyatta-policy-qos-groupings-v1:qlen'] = (
                queue['qlen'] & 0xffffffff)
            queue_out['vyatta-policy-qos-groupings-v1:qlen-packets'] = (
                queue['qlen'])
        else:
            queue_out['vyatta-policy-qos-groupings-v1:qlen-bytes'] = (
                queue['qlen-bytes'])

        queue_out['priority-local'] = queue['prio_local']
        if map_type_values == "dscp-values":
            map_type = "dscp"
        else:
            map_type = "pcp"

        # Not all reverse-map lists may be populated
        try:
            cp_list = reverse_map[tc_id][queue_id]
            queue_out[map_type_values] = convert_map_list(cp_list, map_type)
            tc_queues_out.append(queue_out)

        except KeyError:
            pass

        queue_id += 1

    return tc_queues_out


def convert_tc_queue_list(tc_queues_list_in, reverse_map, map_type_values):
    """
    Convert a 'tc' JSON array into a Yang compatible 'tagged' JSON array
    tagged by traffic-class-id
    """
    tc_queues_list_out = []
    tc_id = 0

    for tc_queues_in in tc_queues_list_in:
        tc_queues_out = {}

        tc_queues_out['traffic-class'] = tc_id
        tc_queues_out['queue-statistics'] = convert_tc_queues(tc_queues_in,
                                                              tc_id,
                                                              reverse_map,
                                                              map_type_values)
        tc_queues_list_out.append(tc_queues_out)
        tc_id += 1

    return tc_queues_list_out


def convert_pipe(cmd, pipe_in, pipe_id, profile_name):
    """
    Convert a single pipe element of a 'pipes' JSON array into a 'tagged'
    element, tagged by pipe-id
    """
    pipe_out = {}

    pipe_out['pipe'] = pipe_id
    pipe_out['qos-class'] = pipe_id
    pipe_out['qos-profile'] = profile_name
    if cmd == 'all':
        # The following elements are defined in
        # vyatta-policy-qos-groupings-v1.yang hence we need to specify
        # their namespace.
        pipe_out['vyatta-policy-qos-groupings-v1:token-bucket-rate'] = (
            pipe_in['params']['tb_rate'])
        pipe_out['vyatta-policy-qos-groupings-v1:token-bucket-size'] = (
            pipe_in['params']['tb_size'])
        pipe_out['vyatta-policy-qos-groupings-v1:traffic-class-period'] = (
            pipe_in['params']['tc_period'])
        pipe_out['vyatta-policy-qos-groupings-v1:traffic-class-rates'] = (
            convert_tc_rates(pipe_in['params']['tc_rates']))
        pipe_out['vyatta-policy-qos-groupings-v1:weighted-round-robin-weights'] = (
            convert_wrr_weights(pipe_in['params']['wrr_weights']))


    (pipe_out['dscp-to-queue-map'], reverse_dscp_map) = (
        convert_dscp_map(pipe_in.get('dscp2q')))

    (pipe_out['pcp-to-queue-map'], reverse_pcp_map) = (
        convert_pcp_map(pipe_in.get('pcp2q')))

    if pipe_out.get('dscp-to-queue-map') is not None:
        queue_list = convert_tc_queue_list(pipe_in['tc'], reverse_dscp_map,
                                           "dscp-values")
    else:
        queue_list = convert_tc_queue_list(pipe_in['tc'], reverse_pcp_map,
                                           "pcp-values")

    pipe_out['traffic-class-queues-list'] = queue_list

    if cmd == 'stats':
        # Throw away the map data if we are processing a 'stats' request
        del pipe_out['dscp-to-queue-map']
        del pipe_out['pcp-to-queue-map']

    return pipe_out


def convert_pipes(cmd, pipes_in, subport_name):
    """
    Convert a 'pipes' JSON array into a Yang compatible 'tagged' JSON array,
    tagged by pipe-id
    """
    pipe_list_out = []
    pipe_id = 0
    policy_name = get_if_subport_policy_name(subport_name)

    if policy_name is None:
        print("policy_name not defined for {}".format(subport_name))
        return None

    for pipe_in in pipes_in:
        profile_name = get_policy_class_profile_name(policy_name, pipe_id)
        if profile_name is not None:
            pipe_out = convert_pipe(cmd, pipe_in, pipe_id, profile_name)
            pipe_list_out.append(pipe_out)

        pipe_id += 1

    return pipe_list_out


def convert_tcs(tcs_in):
    """
    Convert a 'tc' JSON array into a Yang compatible 'tagged' JSON array,
    tagged by traffic-class
    """
    tc_list_out = []
    tc_id = 0

    for tc_in in tcs_in:
        tc_out = {}

        tc_out['traffic-class'] = tc_id

        # Truncate these values for the old 32-bit counters
        # The following counters are defined in vyatta-policy-qos-groupings-v1
        # hence we need to include their namespace
        tc_out['vyatta-policy-qos-groupings-v1:packets'] = tc_in['packets'] & 0xffffffff
        tc_out['vyatta-policy-qos-groupings-v1:bytes'] = tc_in['bytes'] & 0xfffffff

        # The dropped counter is total drops, we just want tail-drops
        tc_out['vyatta-policy-qos-groupings-v1:dropped'] = (
            (tc_in['dropped'] - tc_in['random_drop']) & 0xffffffff)
        tc_out['vyatta-policy-qos-groupings-v1:random-drop'] = (
            tc_in['random_drop'] & 0xffffffff)

        # 64-bit counters don't get truncated
        tc_out['vyatta-policy-qos-groupings-v1:packets-64'] = tc_in['packets']
        tc_out['vyatta-policy-qos-groupings-v1:bytes-64'] = tc_in['bytes']

        # The dropped counter is total drops, we just want tail-drops
        tc_out['vyatta-policy-qos-groupings-v1:dropped-64'] = (
            tc_in['dropped'] - tc_in['random_drop'])
        tc_out['vyatta-policy-qos-groupings-v1:random-drop-64'] = tc_in['random_drop']

        tc_list_out.append(tc_out)
        tc_id += 1

    return tc_list_out


def convert_npf_rule(rules_in):
    """
    Convert the 'rules' JSON dictionary into a Yang compatible 'tagged' JSON
    array, tagged by rule number
    """
    rules_out = []

    for rule_id in rules_in.keys():
        rule_in = rules_in[rule_id]
        rule_operation = rule_in['operation']
        rule_out = {}

        rule_out['rule-number'] = "{}".format(rule_id)
        search_obj = re.search(r'tag\(([0-9]+)\)', rule_operation, flags=0)
        if search_obj:
            rule_out['qos-class'] = "{}".format(search_obj.group(1))

        try:
            rule_out['action-group'] = rule_in['action-group']

        except KeyError:
            rule_out['action-group'] = None

        pstats = rule_in.get('policer-stats')
        if pstats is not None:
            police_stats = pstats.split(' ')
            rule_out['exceeded-packets'] = int(police_stats[2])
            rule_out['exceeded-bytes'] = int(police_stats[4])

        rule_out['packets'] = rule_in['packets']
        rule_out['bytes'] = rule_in['bytes']

        rules_out.append(rule_out)

    return rules_out


def convert_groups(subport_ifname, group_list_in):
    """
    Convert the 'groups' JSON array into a Yang compatible 'tagged' JSON array,
    tagged by 'name' which happens to be the port name
    """
    group_list_out = []

    if group_list_in is not None:
        ifindex = get_sysfs_value(subport_ifname, 'ifindex')

        for group_in in group_list_in:
            group_out = {}

            group_out['name'] = group_in['name']
            group_out['class'] = group_in['class']
            group_out['ifindex'] = ifindex
            group_out['direction'] = group_in['direction']
            # The following element is defined in
            # vyatta-policy-qos-groupings-v1.yang hence we need to specify
            # its namespace.
            group_out['vyatta-policy-qos-groupings-v1:rule'] = convert_npf_rule(group_in['rules'])

            group_list_out.append(group_out)

    return group_list_out


def convert_rules(subport_ifname, rules_in):
    """
    Convert the 'rules' JSON dictionary into a Yang compatible JSON dictionary
    """
    rules_out = {}

    rules_out['groups'] = convert_groups(subport_ifname, rules_in.get('groups'))

    return rules_out


def convert_subports(cmd, subports_in, ifname, vlan_list):
    """
    Convert the 'subports' JSON array into a Yang compatible tagged JSON array,
    tagged by subport-id, subport 0 being the physical port
    """
    subport_list_out = []
    subport_id = 0

    for subport_in in subports_in:
        subport_out = {}
        subport_name = ifname
        subport_ifname = ifname

        if subport_id != 0:
            for vlan in vlan_list:
                if vlan['subport'] == subport_id:
                    vif = vlan['tag']
                    subport_name += " vif {}".format(vif)
                    subport_ifname += ".{}".format(vif)
                    break

        subport_out['subport'] = subport_id
        subport_out['subport-name'] = subport_name
        subport_out['traffic-class-list'] = convert_tcs(subport_in['tc'])
        subport_out['pipe-list'] = convert_pipes(cmd, subport_in['pipes'],
                                                 subport_out['subport-name'])
        subport_out['rules'] = convert_rules(subport_ifname,
                                             subport_in['rules'])

        subport_list_out.append(subport_out)
        subport_id += 1

    return subport_list_out


def convert_vlans(vlans_in):
    """
    Convert the 'vlans' JSON array into a Yang compatible tagged JSON array,
    tagged by the 802.1Q vlan-tag
    """
    vlan_list_out = []

    for vlan in vlans_in:
        vlan_out = {}
        vlan_out['tag'] = vlan['tag']
        vlan_out['subport'] = vlan['subport']

        vlan_list_out.append(vlan_out)

    return vlan_list_out


def convert_shaper(cmd, shaper_in, ifname):
    """
    Convert the 'shaper' JSON dictionary into a Yang compatible JSON dictionary
    """
    shaper_out = {}

    shaper_out['vlan-list'] = convert_vlans(shaper_in['vlans'])
    shaper_out['subport-list'] = convert_subports(cmd, shaper_in['subports'],
                                                  ifname,
                                                  shaper_out['vlan-list'])

    if cmd == 'stats':
        del shaper_out['vlan-list']

    return shaper_out


def convert_if_list(cmd, op_mode_dict):
    """
    Convert the op-mode JSON dictionary generate by the vyatta-dataplane into
    a Yang compatible JSON dictionary

    cmd - either 'all' (full-results) or 'stats' (abbreviated-results)
    op_mode_dict - the op-mode JSON object generated by the vyatta-dataplane

    if_list_out - a tagged JSON array of QoS op-mode state of each physical port
    """
    if_list_out = []
    for ifname in sorted(op_mode_dict.keys()):
        shaper_in = op_mode_dict[ifname]['shaper']

        if_shaper_out = {}
        if_shaper_out['ifname'] = ifname
        if_shaper_out['shaper'] = convert_shaper(cmd, shaper_in, ifname)
        if_list_out.append(if_shaper_out)

    return if_list_out
