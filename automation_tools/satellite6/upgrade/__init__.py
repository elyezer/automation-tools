"""A set of upgrade tasks for upgrading Satellite and Capsule.

Many commands are affected by environment variables. Unless stated otherwise,
all environment variables are required.
"""
import os
import sys
import time
from tasks import (
    create_rhevm_instance, delete_rhevm_instance,
    sync_capsule_tools_repos_to_upgrade
)
from tools import copy_ssh_key, host_pings, reboot
from automation_tools import foreman_debug, set_yum_debug_level, subscribe
from automation_tools.repository import enable_repos, disable_repos
from automation_tools.satellite6.capsule import generate_capsule_certs
from automation_tools.utils import distro_info, update_packages
from fabric.api import env, execute, put, run
if sys.version_info[0] is 2:
    from StringIO import StringIO  # (import-error) pylint:disable=F0401
else:  # pylint:disable=F0401,E0611
    from io import StringIO

# =============================================================================
# Satellite and Capsule Upgrade
# =============================================================================


def satellite6_upgrade(admin_password=None):
    """Upgrades satellite from old version to latest version.

    :param admin_password: A string. Defaults to 'changeme'.
        Foreman admin password for hammer commands.

    The following environment variables affect this command:

    ADMIN_PASSWORD
        Optional, defaults to 'changeme'. Foreman admin password.
    BASE_URL
        Optional, defaults to available satellite version in CDN.
        URL for the compose repository.
    TO_VERSION
        Satellite version to upgrade to and enable repos while upgrading.
        e.g '6.1','6.2'
    """
    to_version = os.environ.get('TO_VERSION')
    if to_version not in ['6.1', '6.2']:
        print('Wrong Satellite Version Provided to upgrade to. '
              'Provide one of 6.1, 6.2')
        sys.exit(1)
    # Sync capsule and tools repo
    if admin_password is None:
        admin_password = os.environ.get('ADMIN_PASSWORD', 'changeme')
    # Setting yum stdout log level to be less verbose
    set_yum_debug_level()
    # Removing rhel-released and rhel-optional repo
    run('rm -rf /etc/yum.repos.d/rhel-{optional,released}.repo')
    print('Wait till Packages update ... ')
    update_packages(quiet=True)
    # Rebooting the system to see possible errors
    if os.environ.get('RHEV_SATELLITE'):
        reboot(120)
    # Setting Satellite61 Repos
    major_ver = distro_info()[1]
    base_url = os.environ.get('BASE_URL')
    # Following disables the old satellite repo and extra repos enabled
    # during subscribe e.g Load balancer
    disable_repos('*', silent=True)
    enable_repos('rhel-{0}-server-rpms'.format(major_ver))
    enable_repos('rhel-server-rhscl-{0}-rpms'.format(major_ver))
    if base_url is None:
        enable_repos('rhel-{0}-server-satellite-{1}-rpms'.format(
            major_ver, to_version))
    else:
        # Add Sat6 repo from latest compose
        satellite_repo = StringIO()
        satellite_repo.write('[sat6]\n')
        satellite_repo.write('name=satellite 6\n')
        satellite_repo.write('baseurl={0}\n'.format(base_url))
        satellite_repo.write('enabled=1\n')
        satellite_repo.write('gpgcheck=0\n')
        put(local_path=satellite_repo,
            remote_path='/etc/yum.repos.d/sat6.repo')
        satellite_repo.close()
    # Stop katello services, except mongod
    run('katello-service stop')
    if to_version == '6.1':
        run('service-wait mongod start')
    run('yum clean all', warn_only=True)
    # Updating the packages again after setting sat6 repo
    print('Wait till packages update ... ')
    print('YUM UPDATE started at: {0}'.format(time.ctime()))
    update_packages(quiet=False)
    print('YUM UPDATE finished at: {0}'.format(time.ctime()))
    # Rebooting the system again for possible errors
    if os.environ.get('RHEV_SATELLITE'):
        reboot(120)
    # Stop the service again which started in restart
    run('katello-service stop')
    if to_version == '6.1':
        run('service-wait mongod start')
    # Running Upgrade
    print('SATELLITE UPGRADE started at: {0}'.format(time.ctime()))
    if to_version == '6.1':
        run('katello-installer --upgrade')
    else:
        run('satellite-installer --scenario satellite --upgrade')
    print('SATELLITE UPGRADE finished at: {0}'.format(time.ctime()))
    # Test the Upgrade is successful
    run('hammer -u admin -p {0} ping'.format(admin_password), warn_only=True)
    # Test The status of all katello services
    run('katello-service status', warn_only=True)


def satellite6_capsule_upgrade(admin_password=None):
    """Upgrades capsule from existing version to latest version.

    :param admin_password: A string. Defaults to 'changeme'.
        Foreman admin password for hammer commands.

    The following environment variables affect this command:

    ADMIN_PASSWORD
        Optional, defaults to 'changeme'. Foreman admin password.
    CAPSULE_URL
        Optional, defaults to available capsule version in CDN.
        URL for capsule of latest compose to upgrade.
    FROM_VERSION
        Capsule current version, to disable repos while upgrading.
        e.g '6.1','6.0'
    TO_VERSION
        Capsule version to upgrade to and enable repos while upgrading.
        e.g '6.1','6.2'

    """
    sat_host = env.get('satellite_host')
    cap_host = env.get('capsule_host')
    from_version = os.environ.get('FROM_VERSION')
    if from_version not in ['6.1', '6.0']:
        print('Wrong Capsule Version Provided. Provide one of 6.1, 6.0.')
        sys.exit(1)
    to_version = os.environ.get('TO_VERSION')
    if to_version not in ['6.1', '6.2']:
        print('Wrong Capsule Version Provided to upgrade to. '
              'Provide one of 6.1, 6.2')
        sys.exit(1)
    if admin_password is None:
        admin_password = os.environ.get('ADMIN_PASSWORD', 'changeme')
    # Setting Capsule61 Repos
    major_ver = distro_info()[1]
    if os.environ.get('CAPSULE_URL') is None:
        enable_repos('rhel-{0}-server-satellite-capsule-{1}-rpms'.format(
            major_ver, to_version))
    disable_repos('rhel-{0}-server-satellite-capsule-{1}-rpms'.format(
        major_ver, from_version))
    # Stop katello services, except mongod
    run('for i in qpidd pulp_workers pulp_celerybeat '
        'pulp_resource_manager httpd; do service $i stop; done')
    run('yum clean all', warn_only=True)
    print('Wait till packages update ... ')
    print('YUM UPDATE started at: {0}'.format(time.ctime()))
    update_packages(quiet=False)
    print('YUM UPDATE finished at: {0}'.format(time.ctime()))
    if from_version == '6.0':
        run('yum install -y capsule-installer', warn_only=True)
        # Copy answer file from katello to capule installer
        run('cp /etc/katello-installer/answers.capsule-installer.yaml.rpmsave '
            '/etc/capsule-installer/answers.capsule-installer.yaml',
            warn_only=True)
    # Generates Capsule Certs file on satelltie and copies in capsule
    execute(
        generate_capsule_certs,
        cap_host,
        True,
        host=sat_host
    )
    # Copying the capsule cert to capsule
    execute(lambda: run("scp -o 'StrictHostKeyChecking no' {0}-certs.tar "
                        "root@{0}:/home/".format(cap_host)), host=sat_host)
    # Rebooting the system again to see possible errors
    if os.environ.get('RHEV_CAPSULE'):
        reboot(120)
    # Stopping the services again which started in reboot
    run('for i in qpidd pulp_workers pulp_celerybeat '
        'pulp_resource_manager httpd; do service $i stop; done')
    # Upgrading Katello installer
    print('CAPSULE UPGRADE started at: {0}'.format(time.ctime()))
    if to_version == '6.1':
        run('capsule-installer --upgrade --certs-tar '
            '/home/{0}-certs.tar'.format(cap_host))
    else:
        run('satellite-installer --scenario capsule --upgrade --certs-tar '
            '/home/{0}-certs.tar'.format(cap_host))
    print('CAPSULE UPGRADE finished at: {0}'.format(time.ctime()))
    # Test The status of all katello services
    run('katello-service status', warn_only=True)


def product_upgrade(
        product, sat_image=None, cap_image=None):
    """Task which upgrades the product.

    Product is satellite or capsule.

    :param product: A string. product name wanted to upgrade.
    :param sat_image: A string. Openstack Satellite image name
        from which instance to create.
    :param cap_image: A string. Openstack Capsule image name
        from which instance to create.

    The following environment variables affect this command:

    RHN_USERNAME
        Red Hat Network username to register the system.
    RHN_PASSWORD
        Red Hat Network password to register the system.
    RHN_POOLID
        Optional. Red Hat Network pool ID. Determines what software will be
        available from RHN.
    ADMIN_PASSWORD
        Optional, defaults to 'changeme'. Foreman admin password.
    BASE_URL
        URL for the compose repository.
    CAPSULE_URL
        The url for capsule repo from latest satellite compose.
        Optional, defaults to latest available capsule version in CDN.
    FROM_VERSION
        The satellite/capsule current version to upgrade to latest.
        e.g '6.1','6.0'
    TO_VERSION
        To which Satellite/Capsule version to upgrade.
        e.g '6.1','6.2'
    OS
        The OS Version on which the satellite is installed.
        e.g 'rhel7','rhel6'
    RHEV_USER
        The username of a rhevm project to login.
    RHEV_PASSWD
        The password of a rhevm project to login.
    RHEV_URL
        An url to API of rhevm project.
    SATELLITE_HOSTNAME
        The Satellite hostname to run upgrade on.
        Optional, If want to run upgrade on specific satellite.
    CAPSULE_HOSTNAME
        The Satellite hostname to run upgrade on.
        Optional, If want to run upgrade on specific capsule.
    CAPSULE_SUBSCRIPTION
        List of cv_name, environment, ak_name attached to subscription of
        capsule in defined sequence.
        Optional, for upgrade on specific satellite and capsule.
    RHEV_SATELLITE
        The Satellite hostname on RHEVM instance.
        Optional, If want to run upgrade on RHEVM instance.
    RHEV_CAPSULE
        The Capsule hostname on RHEVM instance.
        Optional, If want to run upgrade on RHEVM instance.

    """
    products = ['satellite', 'capsule']
    if product not in products:
        print('Product name should be one of {0}'.format(', '.join(products)))
        sys.exit(1)
    if not os.environ.get('SATELLITE_HOSTNAME'):
        if not sat_image and not os.environ.get('RHEV_SATELLITE'):
            print('Please provide either Satellite RHEVM template name or '
                  'Satellite HostName to perform upgrade!')
            sys.exit(1)
        version = os.environ.get('OS')
        if not version:
            print('Please provide OS version as rhel7 or rhel6, And retry !')
            sys.exit(1)
        sat_instance = 'upgrade_satellite_auto_{0}'.format(version)
        # Deleting Satellite instance if any
        execute(delete_rhevm_instance, sat_instance)
        print('Turning on Satellite Instance ....')
        execute(
            create_rhevm_instance,
            sat_instance,
            sat_image
        )
        sat_host = os.environ.get('RHEV_SATELLITE')
        # Wait Till Instance gets up
        host_pings(sat_host)
        # Subscribe the instances to CDN
        execute(subscribe, host=sat_host)
    else:
        sat_host = os.environ.get('SATELLITE_HOSTNAME')
        env['satellite_host'] = sat_host
    # Rebooting the services
    execute(lambda: run('katello-service restart'), host=sat_host)
    # For Capsule Upgrade
    if product == 'capsule':
        if not os.environ.get('CAPSULE_HOSTNAME'):
            if not cap_image and not os.environ.get('RHEV_CAPSULE'):
                print('Please provide either Capsule RHEVM template name or '
                      'Capsule HostName to perform upgrade!')
                sys.exit(1)
            cap_instance = 'upgrade_capsule_auto_{0}'.format(version)
            # Deleting Capsule instance if any
            execute(delete_rhevm_instance, cap_instance)
            print('Turning on Capsule Instance ....')
            execute(
                create_rhevm_instance,
                cap_instance, cap_image
            )
            cap_host = os.environ.get('RHEV_CAPSULE')
        else:
            cap_host = os.environ.get('CAPSULE_HOSTNAME')
            env['capsule_host'] = cap_host
        # Copy ssh key from satellie to capsule
        copy_ssh_key(sat_host, cap_host)
        if os.environ.get('CAPSULE_URL') is not None:
            execute(sync_capsule_tools_repos_to_upgrade, host=sat_host)
    # Run satellite upgrade
    execute(satellite6_upgrade, host=sat_host)
    # Generate foreman debug on satellite
    execute(foreman_debug, 'satellite', host=sat_host)
    if product == 'capsule':
        print('\nRunning Capsule Upgrade ..........')
        # Run capsule upgrade
        execute(satellite6_capsule_upgrade, host=cap_host)
        # Generate foreman debug on capsule
        execute(foreman_debug, 'capsule', host=cap_host)
