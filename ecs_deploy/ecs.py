from datetime import datetime
from json import dumps

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dateutil.tz.tz import tzlocal


class EcsClient(object):
    def __init__(self, access_key_id=None, secret_access_key=None, region=None, profile=None):
        session = boto3.session.Session(aws_access_key_id=access_key_id,
                                        aws_secret_access_key=secret_access_key,
                                        region_name=region,
                                        profile_name=profile)
        self.boto = session.client(u'ecs')

    def describe_services(self, cluster_name, service_name):
        return self.boto.describe_services(cluster=cluster_name, services=[service_name])

    def describe_task_definition(self, task_definition_arn):
        try:
            return self.boto.describe_task_definition(taskDefinition=task_definition_arn)
        except ClientError:
            raise UnknownTaskDefinitionError('Unknown task definition arn: %s' % task_definition_arn)

    def list_tasks(self, cluster_name, service_name):
        return self.boto.list_tasks(cluster=cluster_name, serviceName=service_name)

    def describe_tasks(self, cluster_name, task_arns):
        return self.boto.describe_tasks(cluster=cluster_name, tasks=task_arns)

    def register_task_definition(self, family, containers, volumes, role_arn):
        return self.boto.register_task_definition(
            family=family,
            containerDefinitions=containers,
            volumes=volumes,
            taskRoleArn=role_arn or ''
        )

    def deregister_task_definition(self, task_definition_arn):
        return self.boto.deregister_task_definition(taskDefinition=task_definition_arn)

    def update_service(self, cluster, service, desired_count, task_definition):
        return self.boto.update_service(
            cluster=cluster,
            service=service,
            desiredCount=desired_count,
            taskDefinition=task_definition
        )

    def run_task(self, cluster, task_definition, count, started_by, overrides):
        return self.boto.run_task(
            cluster=cluster,
            taskDefinition=task_definition,
            count=count,
            startedBy=started_by,
            overrides=overrides
        )


class EcsService(dict):
    def __init__(self, cluster, iterable=None, **kwargs):
        self._cluster = cluster
        super(EcsService, self).__init__(iterable, **kwargs)

    def set_desired_count(self, desired_count):
        self[u'desiredCount'] = desired_count

    def set_task_definition(self, task_definition):
        self[u'taskDefinition'] = task_definition.arn

    @property
    def cluster(self):
        return self._cluster

    @property
    def name(self):
        return self.get(u'serviceName')

    @property
    def task_definition(self):
        return self.get(u'taskDefinition')

    @property
    def desired_count(self):
        return self.get(u'desiredCount')

    @property
    def deployment_created_at(self):
        for deployment in self.get(u'deployments'):
            if deployment.get(u'status') == u'PRIMARY':
                return deployment.get(u'createdAt')
        return datetime.now()

    @property
    def deployment_updated_at(self):
        for deployment in self.get(u'deployments'):
            if deployment.get(u'status') == u'PRIMARY':
                return deployment.get(u'updatedAt')
        return datetime.now()

    @property
    def errors(self):
        return self.get_warnings(self.deployment_updated_at)

    @property
    def older_errors(self):
        return self.get_warnings(self.deployment_created_at, self.deployment_updated_at)

    def get_warnings(self, since=None, until=None):
        since = since or self.deployment_updated_at
        until = until or datetime.now(tz=tzlocal())
        errors = {}
        for event in self.get('events'):
            if u'unable' in event[u'message'] and since < event[u'createdAt'] < until:
                errors[event[u'createdAt']] = event[u'message']
        return errors


class EcsTaskDefinition(dict):
    def __init__(self, iterable=None, **kwargs):
        super(EcsTaskDefinition, self).__init__(iterable, **kwargs)
        self._diff = []

    @property
    def containers(self):
        return self.get(u'containerDefinitions')

    @property
    def container_names(self):
        for container in self.get(u'containerDefinitions'):
            yield container[u'name']

    @property
    def volumes(self):
        return self.get(u'volumes')

    @property
    def arn(self):
        return self.get(u'taskDefinitionArn')

    @property
    def family(self):
        return self.get(u'family')

    @property
    def role_arn(self):
        return self.get(u'taskRoleArn')

    @property
    def revision(self):
        return self.get(u'revision')

    @property
    def family_revision(self):
        return '%s:%d' % (self.get(u'family'), self.get(u'revision'))

    @property
    def diff(self):
        return self._diff

    def get_overrides(self):
        override = dict()
        overrides = []
        for diff in self.diff:
            if override.get('name') != diff.container:
                override = dict(name=diff.container)
                overrides.append(override)
            if diff.field == 'command':
                override['command'] = self.get_overrides_command(diff.value)
            elif diff.field == 'environment':
                override['environment'] = self.get_overrides_environment(diff.value)
        return overrides

    def get_overrides_command(self, command):
        return command.split(' ')

    def get_overrides_environment(self, environment_dict):
        return [{"name": e, "value": environment_dict[e]} for e in environment_dict]

    def set_images(self, tag=None, **images):
        self.validate_container_options(**images)
        for container in self.containers:
            if container[u'name'] in images:
                new_image = images[container[u'name']]
                diff = EcsTaskDefinitionDiff(container[u'name'], u'image', new_image, container[u'image'])
                self._diff.append(diff)
                container[u'image'] = new_image
            elif tag:
                image_definition = container[u'image'].rsplit(u':', 1)
                new_image = u'%s:%s' % (image_definition[0], tag.strip())
                diff = EcsTaskDefinitionDiff(container[u'name'], u'image', new_image, container[u'image'])
                self._diff.append(diff)
                container[u'image'] = new_image

    def set_commands(self, **commands):
        self.validate_container_options(**commands)
        for container in self.containers:
            if container[u'name'] in commands:
                new_command = commands[container[u'name']]
                diff = EcsTaskDefinitionDiff(container[u'name'], u'command', new_command, container.get(u'command'))
                self._diff.append(diff)
                container[u'command'] = [new_command]

    def set_environment(self, environment_list):
        environment = {}

        for env in environment_list:
            environment.setdefault(env[0], {})
            environment[env[0]][env[1]] = env[2]

        self.validate_container_options(**environment)
        for container in self.containers:
            if container[u'name'] in environment:
                self.apply_container_environment(container, environment[container[u'name']])

    def apply_container_environment(self, container, new_environment):
        old_environment = {env['name']: env['value'] for env in container.get('environment', {})}
        merged_environment = old_environment.copy()
        merged_environment.update(new_environment)

        diff = EcsTaskDefinitionDiff(container[u'name'], u'environment', merged_environment, old_environment)
        self._diff.append(diff)

        container[u'environment'] = [{"name": e, "value": merged_environment[e]} for e in merged_environment]

    def validate_container_options(self, **container_options):
        for container_name in container_options:
            if container_name not in self.container_names:
                raise UnknownContainerError(u'Unknown container: %s' % container_name)

    def set_role_arn(self, role_arn):
        if role_arn:
            diff = EcsTaskDefinitionDiff(None, u'role_arn', role_arn, self[u'taskRoleArn'])
            self[u'taskRoleArn'] = role_arn
            self._diff.append(diff)


class EcsTaskDefinitionDiff(object):
    def __init__(self, container, field, value, old_value):
        self.container = container
        self.field = field
        self.value = value
        self.old_value = old_value

    def __repr__(self):
        if self.container:
            return u"Changed %s of container '%s' to: %s (was: %s)" % (
                self.field,
                self.container,
                dumps(self.value),
                dumps(self.old_value)
            )
        else:
            return u"Changed %s to: %s (was: %s)" % (
                self.field,
                dumps(self.value),
                dumps(self.old_value)
            )


class EcsAction(object):
    def __init__(self, client, cluster_name, service_name):
        self._client = client
        self._cluster_name = cluster_name
        self._service_name = service_name

        try:
            self._service = self.get_service()
        except IndexError:
            raise ConnectionError(u'An error occurred when calling the DescribeServices operation: Service not found.')
        except ClientError as e:
            raise ConnectionError(str(e))
        except NoCredentialsError:
            raise ConnectionError(u'Unable to locate credentials. Configure credentials by running "aws configure".')

    def get_service(self):
        services_definition = self._client.describe_services(self._cluster_name, self._service_name)
        return EcsService(self._cluster_name, services_definition[u'services'][0])

    def get_current_task_definition(self, service):
        task_definition_payload = self._client.describe_task_definition(service.task_definition)
        task_definition = EcsTaskDefinition(task_definition_payload[u'taskDefinition'])
        return task_definition

    def get_task_definition(self, task_definition):
        task_definition_payload = self._client.describe_task_definition(task_definition)
        task_definition = EcsTaskDefinition(task_definition_payload[u'taskDefinition'])
        return task_definition

    def update_task_definition(self, task_definition):
        response = self._client.register_task_definition(
            task_definition.family,
            task_definition.containers,
            task_definition.volumes,
            task_definition.role_arn
        )
        new_task_definition = EcsTaskDefinition(response[u'taskDefinition'])
        self._client.deregister_task_definition(task_definition.arn)
        return new_task_definition

    def update_service(self, service):
        response = self._client.update_service(service.cluster, service.name, service.desired_count,
                                               service.task_definition)
        return EcsService(self._cluster_name, response[u'service'])

    def is_deployed(self, service):
        if len(service[u'deployments']) != 1:
            return False
        running_tasks = self._client.list_tasks(service.cluster, service.name)
        if not running_tasks[u'taskArns']:
            return service.desired_count == 0
        return service.desired_count == self.get_running_tasks_count(service, running_tasks[u'taskArns'])

    def get_running_tasks_count(self, service, task_arns):
        running_count = 0
        tasks_details = self._client.describe_tasks(self._cluster_name, task_arns)
        for task in tasks_details[u'tasks']:
            if task[u'taskDefinitionArn'] == service.task_definition and task[u'lastStatus'] == u'RUNNING':
                running_count += 1
        return running_count

    @property
    def client(self):
        return self._client

    @property
    def service(self):
        return self._service

    @property
    def cluster_name(self):
        return self._cluster_name

    @property
    def service_name(self):
        return self._service_name


class DeployAction(EcsAction):
    def deploy(self, task_definition):
        self._service.set_task_definition(task_definition)
        return self.update_service(self._service)


class ScaleAction(EcsAction):
    def scale(self, desired_count):
        self._service.set_desired_count(desired_count)
        return self.update_service(self._service)


class RunAction(EcsAction):
    def __init__(self, client, cluster_name):
        self._client = client
        self._cluster_name = cluster_name
        self.started_tasks = []

    def run(self, task_definition, count, started_by):
        result = self._client.run_task(
            cluster=self._cluster_name,
            task_definition=task_definition.family_revision,
            count=count,
            started_by=started_by,
            overrides=dict(containerOverrides=task_definition.get_overrides())
        )
        self.started_tasks = result['tasks']
        return True


class EcsError(Exception):
    pass


class ConnectionError(EcsError):
    pass


class UnknownContainerError(EcsError):
    pass


class TaskPlacementError(EcsError):
    pass


class UnknownTaskDefinitionError(EcsError):
    pass
