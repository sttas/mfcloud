import json
import sys
import inject
from mfcloud.events import EventBus

from twisted.internet import reactor, defer
from twisted.internet.defer import inlineCallbacks

from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketClientFactory
import txredisapi

from twisted.python import log

from autobahn.twisted.websocket import WebSocketClientProtocol, WebSocketServerProtocol


class ApiError(Exception):
    pass



class ApiRpcServer(object):

    redis = inject.attr(txredisapi.Connection)
    eb = inject.attr(EventBus)

    def __init__(self):
        self.tasks = {}
        self.ticket_map = {}

        self.eb.on('log-*', self.on_log)

    def on_log(self, channel, message):
        ticket_id = channel[4:]
        self.task_progress(message, ticket_id)

    def task_completed(self, result, ticket_id):
        if ticket_id in self.ticket_map:
            self.ticket_map[ticket_id].send_event('task.success.%s' % ticket_id, result)

    def task_failed(self, error, ticket_id):
        if ticket_id in self.ticket_map:
            self.ticket_map[ticket_id].send_event('task.failure.%s' % ticket_id, str(error))

    def task_progress(self, data, ticket_id):
        if ticket_id in self.ticket_map:
            log.msg('Progress: %s' % data)
            self.ticket_map[ticket_id].send_event('task.progress.%s' % ticket_id, data)

    @inlineCallbacks
    def task_start(self, client, task_name, *args, **kwargs):
        """
        Return all passed args.
        """
        ticket_id = yield self.redis.incr('mfcloud-ticket-id')

        def _do_start():

            self.eb.fire_event('task.start', data={
                'name': task_name,
                'args': args,
                'kwargs': kwargs
            })

            if not task_name in self.tasks:
                raise ValueError('No such task: %s' % task_name)

            try:
                task_defered = self.tasks[task_name](ticket_id, *args, **kwargs)

                task_defered.addCallback(self.task_completed, ticket_id)
                task_defered.addErrback(self.task_failed, ticket_id)

            except Exception as e:
                self.task_failed(e.message, ticket_id)

        reactor.callLater(0, _do_start)

        self.ticket_map[ticket_id] = client
        defer.returnValue(json.dumps({'success': True, 'id': ticket_id}))

    def xmlrpc_is_completed(self, ticket_id):
        d = self.redis.get('mfcloud-ticket-%s-completed' % ticket_id)

        def on_result(result):
            return result == 1

        d.addCallback(on_result)
        return d

    def xmlrpc_get_result(self, ticket_id):
        d = self.redis.get('mfcloud-ticket-%s-result' % ticket_id)
        d.addCallback(lambda result: json.loads(result))
        return d



class MdcloudWebsocketServerProtocol(WebSocketServerProtocol):
    def __init__(self):
        pass

    def onConnect(self, request):
        pass

    def onOpen(self):
        self.factory.server.on_client_connect(self)

    def onClose(self, wasClean, code, reason):
        self.factory.server.on_client_disconnect(self, wasClean, code, reason)

    def onMessage(self, payload, isBinary):

        log.msg('Websocket server in: %s' % payload)
        #print(self.factory.server.on_message())

        reactor.callLater(0, self.factory.server.on_message, self, payload, isBinary)

    def send_event(self, event_name, data=None):
        data_ = {'type': 'event', 'name': event_name, 'data': data}
        log.msg('Sent out event: %s' % event_name)
        return self.sendMessage(json.dumps(data_))

    def send_response(self, request_id, response, success=True):
        data_ = {'type': 'response', 'id': request_id, 'success': success, 'response': response}
        log.msg('Sent out response: %s' % request_id)
        return self.sendMessage(json.dumps(data_))


class Server(object):
    redis = inject.attr(txredisapi.Connection)
    eb = inject.attr(EventBus)
    rpc_server = inject.attr(ApiRpcServer)

    settings = inject.attr('settings')

    def __init__(self, port=7080):
        self.task_map = {}
        self.port = port
        self.clients = []


    @inlineCallbacks
    def on_message(self, client, payload, is_binary=False):
        #"""
        #Method is called when new message arrives from client
        #"""
        #ticket_id = yield self.redis.incr('mfcloud-ticket-id')

        log.msg('Incomming message: %s' % payload)

        try:
            data = json.loads(payload)

            if data['task'] == 'ping':
                yield client.send_response(data['id'], 'pong')

            elif data['task'] == 'task_start':
                ticket_id = yield self.rpc_server.task_start(client, *data['args'], **data['kwargs'])
                yield client.send_response(data['id'], ticket_id)
            else:
                yield client.send_response(data['id'], 'Unknown command', success=False)

        except Exception:
            log.err()



    def on_client_connect(self, client):
        """
        Method is called when new client is here
        """
        self.clients.append(client)
        log.msg('Client connected')

    def on_client_disconnect(self, client, wasClean, code, reason):
        """
        Method is called when client disconnects
        """
        if client in self.clients:
            self.clients.remove(client)

        log.msg('Client disconnected')

    def shutdown(self):
        """
        Terminate all client sessions
        """
        for protocol in self.clients:
            protocol.sendClose()

        self.clients = []

    def register_task(self, name, callback):
        """
        Add new task to task list
        """
        self.tasks[name] = callback

    def verifyCallback(self, connection, x509, errnum, errdepth, ok):

        if not ok:
            print 'invalid cert from subject:', x509.get_subject()
            return False
        else:
            print 'Subject is: %s' % x509.get_subject().commonName
            print "Certs are fine"
            # return False
        return True

    def bind(self):
        """
        Start listening on the port specified
        """
        factory = WebSocketServerFactory("ws://localhost:%s" % self.port, debug=False)
        factory.noisy = False
        factory.server = self
        factory.protocol = MdcloudWebsocketServerProtocol

        try:

            if self.settings.ssl.enabled:

                from OpenSSL import SSL

                from twisted.internet import ssl
                myContextFactory = ssl.DefaultOpenSSLContextFactory(
                    self.settings.ssl.key, self.settings.ssl.cert
                )
                ctx = myContextFactory.getContext()

                ctx.set_verify(SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, self.verifyCallback)

                # Since we have self-signed certs we have to explicitly
                # tell the server to trust them.
                ctx.load_verify_locations(self.settings.ssl.cert)

                reactor.listenSSL(self.port, factory, myContextFactory)
            else:
                reactor.listenTCP(self.port, factory)
        except:
            log.err()





class MdcloudWebsocketClientProtocol(WebSocketClientProtocol):
    def __init__(self):
        pass

    client = None

    def onConnect(self, response):
        pass

    def onOpen(self):
        self.client.protocol = self
        self.client.onc.callback(True)

    def onMessage(self, payload, is_binary):
        """
        is_binary affects method of message encoding:

        if is_binary:
            print("Binary message received: {0} bytes".format(len(payload)))
        else:
            print("Text message received: {0}".format(payload.decode('utf8')))
        """

        log.msg('Websocket client in: %s' % payload)
        reactor.callLater(0, self.client.on_message, payload, is_binary)

    def onClose(self, wasClean, code, reason):
        self.client.onClose(wasClean, code, reason)


class Client(object):

    def __init__(self, host='127.0.0.1', port=7080, settings=None):
        self.port = port
        self.host = host
        self.onc = None
        self.protocol = None
        self.request_id = 0
        self.request_map = {}
        self.settings = settings

        self.task_map = {}

    def send(self, data):
        log.msg('Send message: %s' % data)
        return self.protocol.sendMessage(data)

    def shutdown(self):
        self.protocol.sendClose()

    def on_message(self, data, is_binary=False):

        log.msg('Client in: %s' % data)

        try:
            data = json.loads(data)
        except ValueError:
            raise Exception('Invalid json: %s' % data)

        if data['type'] == 'response':
            if data['id'] in self.request_map:
                if data['success']:
                    return self.request_map[data['id']].callback(data['response'])
                else:
                    return self.request_map[data['id']].errback(ApiError(data['response']))
            else:
                raise Exception('Unknown request id: %s' % data['id'])

        if data['type'] == 'event':

            events = ['progress', 'failure', 'success']

            if data['name'].startswith('task.'):

                etype, task_id = data['name'].split('.')[1:]

                task_id = int(task_id)

                if not etype in events:
                    raise Exception('Unknown task event: %s' % etype)

                if task_id in self.task_map:
                    method = 'on_%s' % etype
                    # call one of on_progress, on_failure, on_success
                    getattr(self.task_map[task_id], method)(data['data'])
                else:
                    raise Exception('Unknown task id: %s' % task_id)

    def connect(self):
        factory = WebSocketClientFactory("ws://%s:%s" % (self.host, self.port), debug=False)
        factory.noisy = False
        factory.protocol = MdcloudWebsocketClientProtocol
        factory.protocol.client = self

        self.onc = defer.Deferred()

        if self.settings.ssl.enabled:
            from mfcloud.ssl import CtxFactory
            reactor.connectSSL(self.host, self.port, factory, CtxFactory())
        else:
            reactor.connectTCP(self.host, self.port, factory)

        return self.onc

    def call_sync(self, task, *args, **kwargs):
        d = defer.Deferred()

        self.request_id += 1
        _id = self.request_id

        self.request_map[_id] = d

        msg = {
            'id': _id,
            'task': task,
            'args': args,
            'kwargs': kwargs,
        }

        self.send(json.dumps(msg))

        return d

    def onClose(self, wasClean, code, reason):

        if not wasClean:
            print('Connection closed: %s (code: %s)' % (reason, code))

        # reactor.stop()


    @inlineCallbacks
    def call(self, task, *args, **kwargs):

        result = yield self.call_sync('task_start', task.name, *args, **kwargs)
        result = json.loads(result)

        if result['success']:
            task.id = result['id']
        else:
            raise ApiError(result['error'])

        task.is_running = True

        self.task_map[task.id] = task

        defer.returnValue(task)



class TaskFailure(Exception):
    pass


class Task(object):

    def __init__(self, name):
        super(Task, self).__init__()

        self.name = name
        self.id = None
        self.is_running = False

        self.data = []
        self.response = None
        self.failure = False

        self.wait = None

    def on_progress(self, data):
        self.data.append(data)

    def on_success(self, result):
        self.response = result
        self.is_running = False
        if self.wait:
            self.wait.callback(result)

    def on_failure(self, result):
        self.is_running = False
        self.response = result
        self.failure = True
        if self.wait:
            self.wait.errback(TaskFailure(result))

    def wait_result(self):
        self.wait = defer.Deferred()
        return self.wait

if __name__ == '__main__':
    from twisted.python import log

    log.startLogging(sys.stdout)

    server = Server(port=9999)
    server.bind()

    reactor.run()