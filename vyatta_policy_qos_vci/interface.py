#!/usr/bin/env python3
"""
A module to define the Interface class of objects
"""
# Copyright (c) 2019-2020, AT&T Intellectual Property.
# All rights reserved.
#
# SPDX-License-Identifier: LGPL-2.1-only
#

import logging

from vyatta_policy_qos_vci.ingress_map_binding import IngressMapBinding
from vyatta_policy_qos_vci.subport import Subport

LOG = logging.getLogger('Policy QoS VCI')

POLICY_KEY = {
    'bonding': 'vyatta-interfaces-bonding-qos-v1',
    'dataplane': 'vyatta-policy-qos-v1',
    'vhost': 'vyatta-interfaces-vhost-qos-v1'
}

class Interface:
    """
    A class for interface objects.  We will have one Interface object for
    each physical port that has QoS configured on it.
    """
    def __init__(self, if_type, if_dict, qos_policy_dict, ingress_map_dict):
        """
        Create an interface object for a physical port
        if_type is one of "dataplane", "bonding" or "vhost"
        """
        self._if_dict = if_dict
        if if_type == 'vhost':
            self._name = if_dict.get('name')
            policy_namespace = 'vyatta-interfaces-vhost-policy-v1'
            vif_namespace = 'vyatta-interfaces-vhost-vif-v1:'
        else:
            self._name = if_dict.get('tagnode')
            policy_namespace = 'vyatta-interfaces-policy-v1'
            vif_namespace = ''
        self._ifindex = None
        self._subports = []
        self._ingress_map_bindings = []
        self._policies = []
        self._profile_index = {}

        if_policy_dict = None
        port_params_dict = None
        # Get the trunk's QoS policy name
        try:
            # Try the normal vyatta VM style
            if_policy_dict = if_dict[f"{policy_namespace}:policy"]

        except KeyError:
            try:
                # Try the hardware-switch platform style
                if_policy_dict = if_dict['vyatta-interfaces-dataplane-switch-v1:switch-group']
                port_params_dict = if_policy_dict['port-parameters']
                if_policy_dict = port_params_dict['vyatta-interfaces-switch-policy-v1:policy']
            except KeyError:
                pass

        # We have three different namespaces choices to deal with:
        # vyatta-policy-qos-v1 - the standard and switch qos namespace
        # vyatta-interfaces-bonding-qos-v1 - for bonded interfaces
        # vyatta-interfaces-vhost-qos-v1 - for vhost interfaces
        namespace = POLICY_KEY[if_type]
        if_policy_name = if_policy_dict.get(f'{namespace}:qos')
        policy = None
        if if_policy_name is not None:
            policy = qos_policy_dict[if_policy_name]

        if policy is not None:
            subport = Subport(self, 0, 0, policy)
            self._subports.append(subport)
            # cross-link the policy and the interface
            self._policies.append(policy)
            policy.add_interface(self)

        try:
            ingress_map_name = if_policy_dict['vyatta-policy-qos-v1:ingress-map']
            ingress_map = ingress_map_dict[ingress_map_name]
            binding = IngressMapBinding(self, 0, ingress_map)
            # cross-link the ingress-map and the binding
            self._ingress_map_bindings.append(binding)
            ingress_map.add_binding(binding)

        except KeyError:
            # Maybe there is no ingress map for this interface
            pass

        # Look for subports

        # Try the normal vyatta VM style
        vif_list = if_dict.get(f"{vif_namespace}vif")
        if vif_list is not None:
            subport_id = 1
            for vif in vif_list:
                vlan_id = vif['tagnode']
                if_policy_dict = vif[f"{policy_namespace}:policy"]
                if_policy_name = if_policy_dict.get(f'{namespace}:qos')
                policy = None
                if if_policy_name is not None:
                    policy = qos_policy_dict[if_policy_name]

                subport = Subport(self, subport_id, vlan_id, policy)
                self._subports.append(subport)
                if policy is not None:
                    # cross-link the policy and interface
                    self._policies.append(policy)
                    policy.add_interface(self)

                # no ingress-maps on normal vyatta VMs
                subport_id += 1

        # Try the SIAD hardware-switch platform style
        vlan_list = None
        if port_params_dict is not None:
            try:
                vlan_params_dict = port_params_dict['vlan-parameters']
                qos_params_dict = vlan_params_dict['qos-parameters']
                vlan_list = qos_params_dict['vlan']

            except KeyError:
                pass

        if vlan_list is not None:
            subport_id = 1
            for vlan in vlan_list:
                vlan_id = vlan['vlan-id']
                if_policy_dict = vlan['vyatta-interfaces-switch-policy-v1:policy']
                if_policy_name = if_policy_dict.get(f'{namespace}:qos')
                policy = None
                if if_policy_name is not None:
                    policy = qos_policy_dict[if_policy_name]

                if policy is not None:
                    subport = Subport(self, subport_id, vlan_id, policy)
                    self._subports.append(subport)
                    # cross-link the policy and interface
                    self._policies.append(policy)
                    policy.add_interface(self)
                    subport_id += 1

                try:
                    ingress_map_name = if_policy_dict['vyatta-policy-qos-v1:ingress-map']
                    ingress_map = ingress_map_dict[ingress_map_name]
                    binding = IngressMapBinding(self, vlan_id, ingress_map)
                    # cross-link the ingress-map and the binding
                    self._ingress_map_bindings.append(binding)
                    ingress_map.add_binding(binding)

                except KeyError:
                    # Maybe there's no ingress map for this vlan
                    pass


        for subport in self._subports:
            subport.build_profile_index(self)

    def __eq__(self, interface):
        """ Compare the original JSON dictionaires of two interfaces """
        if self._if_dict == interface.if_dict:
            return True

        return False

    @property
    def if_dict(self):
        """ Return the original JSON for this interface """
        return self._if_dict

    @property
    def name(self):
        """ Return the name of this interface """
        return self._name

    def profile_index_get(self, key):
        """
        Get the named profile's index.
        Each interface gets a profile-index dictionary built for it.
        The first profile added to the profile-index gets an index of
        zero, the second profile an index of one, and so on.
        The global profiles get added to the dictionary first, and
        hence have the lowest indicies.
        The key for each global profile is "global <profile-name>"
        Then each subport policy on the interface adds its local
        profiles to the profile-index.
        The key for each subport profile is "<vlan-id> <profile-name>".
        The trunk port is identified by vlan-id 0.
        So we could end up with the following profile-index:
        {"global bill": 0,
         "global fred": 1,
         "0 paul": 2,
         "0 bert": 3,
         "10 alan": 4,
         "20 pete": 5}
        """
        return self._profile_index.get(key)

    def profile_index_set(self, key, value):
        """ Set the named profile's index """
        self._profile_index[key] = value

    @property
    def profile_index_size(self):
        """ How many profiles does this interface have? """
        return len(self._profile_index)

    @property
    def ifindex(self):
        """
        Return the ifindex for this interface.
        If we don't have it, get it from the appropriate system file.
        """
        if self._ifindex is not None:
            return self._ifindex

        filename = f"/sys/class/net/{self._name}/ifindex"
        try:
            with open(filename) as if_file:
                self._ifindex = if_file.read().replace('\n', '')

        except OSError:
            # If we can't open the file, then the interface in question
            # is probably a deferred interface, e.g. a vhost interface.
            LOG.debug(f"Failed to open {filename} for {self._name}")

        return self._ifindex

    @property
    def policies(self):
        """
        Return the list of policies (trunk and vlan) attached to this
        interface
        """
        return self._policies

    @property
    def ingress_map_bindings(self):
        """
        Return the list of ingress-maps that are bound to this interface or
        any vlans associated with this interface.
        """
        return self._ingress_map_bindings

    def commands(self):
        """
        Issue the QoS config to the vyatta-dataplane commands for QoS policy
        attached to this interface.
        """
        cmd_list = []
        cmd_prefix = f"qos {self.ifindex}"
        max_subports = len(self._subports)
        max_pipes = 0
        for subport in self._subports:
            if subport.policy is not None:
                max_pipes = subport.policy.max_pipes(max_pipes)
                # Only the trunk policy on subport 0 has a frame-overhead
                if subport.id == 0:
                    overhead = subport.policy.overhead

        if max_pipes != 0:
            cmd = (f"{cmd_prefix} port subports {max_subports} "
                   f"pipes {max_pipes} profiles {self.profile_index_size} "
                   f"overhead {overhead}")
            cmd_list.append(cmd)


        for subport in self._subports:
            cmd_list += subport.commands(self)

        if max_pipes != 0:
            cmd_list.append(f"{cmd_prefix} enable")

        return cmd_list
