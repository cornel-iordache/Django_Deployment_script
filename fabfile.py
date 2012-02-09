# -*- coding: utf-8 -*-
#
# Name: Fabric deployment script for Django applications
# Description: Fabric script for deploying your Django applications
# Author: Cornel Iordache
# Based on: Gareth Rushgrove deployment script (http://morethanseven.net/2009/07/27/fabric-django-git-apache-mod_wsgi-virtualenv-and-p/)
#
# Your project directory structure could look something like this:
# project_name/
#    app1/
#        __init__.py
#        admin.py
#        models.py
#        tests.py
#        ....
#    app2/
#        __init__.py
#        admin.py
#        ...
#    __init__.py
#    manage.py
#    settings.py
#    urls.py
#    ...
#    project_name.wsgi

import os
import sys
import posixpath

from fabric.api import env, local, run, sudo, put, cd, runs_once, prompt, require, settings
from fabric.contrib.files import exists, upload_template
from fabric.contrib.console import confirm
from fabric.context_managers import hide

# Global settings
env.project_name = 'tilecache_server' # Project name
env.project_domain = 'http://ec2-107-22-101-154.compute-1.amazonaws.com' # Project domain
env.project_directory = '/home/osm/tilecache/src' # Local project working directory

# Environments
def production():
    "Production environment"
    
    # General settings
    env.hosts = ['ec2-107-22-101-154.compute-1.amazonaws.com:4444'] # One or multiple server addresses in format ip:port
    env.path = '/mnt/osm/tilecache' # Path where your application will be deployed
    env.user = 'cornel' # Username used when making SSH connections
    env.www_user = 'www' # User account under which Nginx is running
    env.password = 'Ec2_08C79I15' # Connection and sudo password (you can omit it and Fabric will prompt you when necessary)
    env.shell = '/usr/local/bin/bash -l -c' # Path to your shell binary
    env.sudo_prompt = 'Password:' # Sudo password prompt
    
    # Database settings
    env.db_hostname = 'localhost'
    env.db_username = 'OSM'
    env.db_password = 'A12_cYhW_140I'
    env.db_name = 'BlueMarble'
    env.db_file = 'db_gis.sql'

# Tasks
def run_tests():
    "Run the test suite"
    
    local('python %(project_name)s/manage.py test' % {'project_name': env.project_name})
    
def get_django_from_svn():
    "Download the latest Django release from SVN"
    require('/mnt/osm/Django')
        
    run('cd %(path)s; svn co http://code.djangoproject.com/svn/django/trunk/ django-trunk' % {'path': env.path})
    run('ln -s %(path)s/django-trunk/django %(path)s/lib/python2.6/site-packages/django' % {'path': env.path})
    
def update_django_from_svn():
    "Update the local Django SVN release"
    require('/mnt/osm/Django')
        
    sudo('cd %(path)s/django-trunk; svn update' % {'path': env.path})

def setup():
    "Create a new Python virtual environment and folders where our application will be saved"
    require('localhost', provided_by = [production])
    require('/mnt/osm/Django')
    
    sudo('easy_install pip')
    sudo('pip install virtualenv')
    sudo('mkdir -p %(path)s; cd %(path)s; virtualenv --no-site-packages .'  % {'path': env.path})
    sudo('chown -R %(user)s:%(user)s %(path)s'  % {'user': env.user, 'path': env.path})
    run('cd %(path)s; mkdir releases; mkdir packages' % {'path': env.path})

def deploy_site():
    """
    Deploy the latest version of the site to the server(s), install any
    required third party modules, install the virtual hosts and 
    then reload the Nginx  and Supervisor
    """
    require('hosts', provided_by = [production])
    require('path')

    import time
    env.release = time.strftime('%Y%m%d%H%M%S')

    _upload_archive_from_git()
    _install_dependencies()
    _install_site()
    _symlink_current_release()
    _create_database_schema()
    _reload_nginx()
    _reload_supervisorctl()
    
def deploy_database():
    """
    Deploy the database (import data located in db_file)
    """
    require('db_hostname', 'db_username', 'db_password', 'db_name', 'db_file')
    require('release', provided_by = [deploy_site, setup])
    
    run('mysql -h %(db_hostname)s -u %(db_username)s -p%(db_password)s %(db_name)s < %(path)s/releases/%(release)s/other/%(db_file)s' % {'path': env.path, 'release': env.release, 'db_hostname': env.db_hostname, 'db_username': env.db_username, 'db_password': env.db_password, 'db_name': env.db_name, 'db_file': env.db_file})
    run('rm %(path)s/releases/%(release)s/other/%(db_file)s' % {'path': env.path, 'release': env.release, 'db_file': env.db_file})
    
def deploy_release(release):
    "Specify a specific release to be made live"
    require('hosts', provided_by = [production])
    require('path')
    
    env.release = release
    run('cd %(path)s; rm releases/previous; mv releases/current releases/previous;'  % {'path': env.path})
    run('cd %(path)s; ln -s %(release)s releases/current'  % {'path': env.path, 'release': env.release})
    
    _reload_nginx()

def rollback():
    """
    Limited rollback capability. Simple loads the previously current
    version of the code. Rolling back again will swap between the two.
    """
    require('hosts', provided_by = [production])
    require('path')

    run('cd %(path)s; mv releases/current releases/_previous;' % {'path': env.path})
    run('cd %(path)s; mv releases/previous releases/current;' % {'path': env.path})
    run('cd %(path)s; mv releases/_previous releases/previous;' % {'path': env.path})
    
    _reload_nginx()
    
def cleanup():
    """
    Clean up the remote environment.
    Flush the database, delete the Apache and lighttpd vhosts, uninstall
    installed dependencies and remove everything from directory packages, releases and other
    """
    
    with settings(hide('warnings', 'stderr', 'stdout'), warn_only = True):
        # Flush the database
        run('cd %(path)s/releases/current/%(project_name)s; ../../../bin/python manage.py flush --noinput' % {'path': env.path, 'project_name': env.project_name})
        
        # Delete the Apache and lighttpd vhost config files
        sudo('rm /usr/local/etc/nginx/sites-available/%(project_domain)s'  % {'project_domain': env.project_domain})
        sudo('rm /usr/local/etc/nginx/sites-enabled/%(project_domain)s' % {'project_domain': env.project_domain})
        sudo('rm /usr/local/etc/nginx/%(project_domain)s.conf' % {'project_domain': env.project_domain})
        
        # Remove the include statement from the lighttpd config file for our vhost
        sudo('sed \'/\/usr\/local\/etc\/lighttpd\/%(project_domain)s.conf/d\' /usr/local/etc/lighttpd.conf > /usr/local/etc/lighttpd.conf.1; mv /usr/local/etc/lighttpd.conf.1 /usr/local/etc/lighttpd.conf' % {'project_domain': env.project_domain})
     
        # Uninstall installed dependencies
        run('cd %(path)s; pip uninstall -E . -r ./releases/current/dependencies.txt -y' % {'path': env.path})
        
        # Remove directory packages, releases and other (if exists)
        sudo('rm -rf %(path)s/packages/'  % {'path': env.path})
        sudo('rm -rf %(path)s/releases/' % {'path': env.path})
        sudo('rm -rf %(path)s/other/' % {'path': env.path})
    
# Helpers - these are called by other functions rather than directly
def _upload_archive_from_git():
    "Create an archive from the current Git master branch and upload it"
    require('release', provided_by = [deploy_site, setup])
    
    local('git archive --format=zip master > %(release)s.zip' % {'release': env.release})
    run('mkdir %(path)s/releases/%(release)s' % {'path': env.path, 'release': env.release})
    put('%(release)s.zip' % {'release': env.release}, '%(path)s/packages/' % {'path': env.path})
    run('cd %(path)s/releases/%(release)s && tar zxf ../../packages/%(release)s.tar' % {'path': env.path, 'release': env.release})
    local('rm %(release)s.tar' % {'release': env.release})

def _install_site():
    "Add the virtualhost to nginx and supervisor and move the production settings config file"
    require('release', provided_by = [deploy_site, setup])
    
    # Move files to their final desination
    run('cd %(path)s/releases/%(release)s; mv other/dependencies.txt dependencies.txt' % {'path': env.path, 'release': env.release})
    run('cd %(path)s/releases/%(release)s; mv other/%(project_name)s.wsgi %(project_name)s/%(project_name)s.wsgi' % {'path': env.path, 'release': env.release, 'project_name': env.project_name})
    
    # Nginx
    sudo('cd %(path)s/releases/%(release)s; cp other/%(project_name)s.nginx /usr/local/etc/nginx/sites-available/%(project_domain)s' % {'path': env.path, 'release': env.release, 'project_name': env.project_name, 'project_domain': env.project_domain})
    sudo('ln -s /usr/local/etc/nginx/sites-available/%(project_domain)s /usr/local/etc/nginx/sites-enabled/%(project_domain)s' % {'project_domain': env.project_domain, 'project_name': env.project_name}) 
    
    # Supervisor
    sudo('cd %(path)s/releases/%(release)s; cp other/%(project_name)s.supervisor /usr/local/etc/supervisor/%(project_domain)s.conf' % {'path': env.path, 'release': env.release, 'project_name': env.project_name, 'project_domain': env.project_domain})
 
    # There are some problems with quote escaping in sudo, run and local functions, so we first store a line which will be appended to the config in a local file
    # and later on remove the added backslashes from the lighttpd config file
    local('echo include "/usr/local/etc/supervisor/%(project_domain)s.conf" >> vhost_file_path.tmp' % {'project_domain': env.project_domain})
    put('vhost_file_path.tmp', '%(path)s/vhost_file_path.tmp' % {'path': env.path})
    local('rm vhost_file_path.tmp')
    
    sudo('cd %(path)s; cat vhost_file_path.tmp >> /usr/local/etc/supervisorctl.conf; rm vhost_file_path.tmp' % {'path': env.path})
    sudo('sed \'s/\\\//g\' /usr/local/etc/supervisorctl.conf > /usr/local/etc/supervisorctl.conf.1; mv /usr/local/etc/supervisorctl.conf.1 /usr/local/etc/supervisorctl.conf')
    
    # Move the production settings.py file
    sudo('cd %(path)s/releases/%(release)s/other; mv settings.py %(path)s/releases/%(release)s/%(project_name)s/settings.py' % {'path': env.path, 'release': env.release, 'project_name': env.project_name})
    
    run('cd %(path)s/releases/%(release)s; rm -rf other/' % {'path': env.path, 'release': env.release})
    sudo('chown -R %(www_user)s:%(www_user)s %(path)s/releases/%(release)s' % {'www_user': env.www_user, 'path': env.path, 'release': env.release})

def _install_dependencies():
    "Install the required packages from the requirements file using PIP" 
    require('release', provided_by = [deploy_site, setup])

    run('cd %(path)s; pip install -E . -r ./releases/%(release)s/other/dependencies.txt' % {'path': env.path, 'release': env.release})

def _symlink_current_release():
    "Symlink our current release"
    require('release', provided_by = [deploy_site, setup])

    # Don't print warrnings if there is no current release
    with settings(hide('warnings', 'stderr'), warn_only = True):
        run('cd %(path)s; rm releases/previous; mv releases/current releases/previous' % {'path': env.path }) 
    
    run('cd %(path)s; ln -s %(release)s releases/current' % {'path': env.path, 'release': env.release})
    
def _create_database_schema():
    "Create the database tables for all apps in INSTALLED_APPS whose tables have not already been created"
    require('project_name')
    
    run('cd %(path)s/releases/current/%(project_name)s; ../../../bin/python manage.py syncdb --noinput' % {'path': env.path, 'project_name': env.project_name})

def _reload_nginx():
    "Reload the apache server"
    sudo('/usr/local/etc/rc.d/apache22 reload')

def _reload_tilecache():
    "Reload the tilecache server"
    sudo('/usr/local/etc/rc.d/tilecache reload')
