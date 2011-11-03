import logging
import os
import Queue
import signal
import sys
import time
import threading
from logging.handlers import RotatingFileHandler

from skew.exceptions import QueueException
from skew.queue import Invoker
from skew.registry import registry
from skew.utils import load_class


class IterableQueue(Queue.Queue):
    def __iter__(self):
        return self
    
    def next(self):
        result = self.get()
        if result is StopIteration:
            raise result
        return result


class Consumer(object):
    def __init__(self, invoker, config):
        self.invoker = invoker
        self.config = config
        
        self.logfile = config.LOGFILE
        self.loglevel = config.LOGLEVEL
        
        self.threads = config.THREADS
        self.periodic_commands = config.PERIODIC

        self.default_delay = config.INITIAL_DELAY
        self.backoff_factor = config.BACKOFF
        self.max_delay = config.MAX_DELAY
        
        # initialize delay
        self.delay = self.default_delay
        
        self.logger = self.get_logger()
        
        # queue to track messages to be processed
        self._queue = IterableQueue()
        self._pool = threading.BoundedSemaphore(self.threads)
        
        self._shutdown = threading.Event()
    
    def get_logger(self):
        log = logging.getLogger('skew.consumer.logger')
        log.setLevel(self.loglevel)
        
        if not log.handlers:
            handler = RotatingFileHandler(self.logfile, maxBytes=1024*1024, backupCount=3)
            handler.setFormatter(logging.Formatter("%(asctime)s:%(name)s:%(levelname)s:%(message)s"))
            
            log.addHandler(handler)
        
        return log
    
    def spawn(self, func, *args, **kwargs):
        t = threading.Thread(target=func, args=args, kwargs=kwargs)
        t.daemon = True
        t.start()
        return t
    
    def start_periodic_command_thread(self):
        self.logger.info('Starting periodic command execution thread')
        return self.spawn(self.enqueue_periodic_commands)

    def enqueue_periodic_commands(self):
        while 1:
            start = time.time()
            self.logger.debug('Enqueueing periodic commands')
            
            try:
                self.invoker.enqueue_periodic_commands()
            except:
                self.logger.error('Error enqueueing periodic commands', exc_info=1)
            
            time.sleep(60 - (time.time() - start))
    
    def start_processor(self):
        self.logger.info('Starting processor thread')
        return self.spawn(self.processor)
    
    def processor(self):
        while not self._shutdown.is_set():
            self.process_message()
    
    def process_message(self):
        message = self.invoker.read()
        
        if message:
            self._pool.acquire()
            
            self.logger.info('Processing: %s' % message)
            self.delay = self.default_delay
            
            # put the message into the queue for the scheduler
            self._queue.put(message)
            
            # wait to acknowledge receipt of the message
            self.logger.debug('Waiting for receipt of message')
            self._queue.join()
        else:
            if self.delay > self.max_delay:
                self.delay = self.max_delay
            
            self.logger.debug('No messages, sleeping for: %s' % self.delay)
            
            time.sleep(self.delay)
            self.delay *= self.backoff_factor
    
    def start_scheduler(self):
        self.logger.info('Starting scheduler thread')
        return self.spawn(self.scheduler)
    
    def scheduler(self):
        for job in self._queue:
            # spin up a worker with the given job
            self.spawn(self.worker, job)
    
    def worker(self, message):        
        # indicate receipt of the task
        self._queue.task_done()
        
        try:
            command = registry.get_command_for_message(message)
            command.execute()
        except QueueException:
            # log error
            self.logger.warn('queue exception raised', exc_info=1)
        except:
            # log the error and raise, killing the worker
            self.logger.error('unhandled exception in worker thread', exc_info=1)
        finally:
            self._pool.release()
    
    def start(self):
        if self.periodic_commands:
            self.start_periodic_command_thread()
        
        self._scheduler = self.start_scheduler()
        self._processor = self.start_processor()
    
    def shutdown(self):
        self._shutdown.set()
        self._queue.put(StopIteration)
    
    def handle_signal(self, sig_num, frame):
        self.logger.info('Received SIGTERM, shutting down')
        self.shutdown()
    
    def set_signal_handler(self):
        self.logger.info('Setting signal handler')
        signal.signal(signal.SIGTERM, self.handle_signal)

    def log_registered_commands(self):
        msg = ['Skew initialized with following commands']
        for command in registry._registry:
            msg.append('+ %s' % command)

        self.logger.info('\n'.join(msg))
    
    def run(self):
        self.set_signal_handler()

        self.log_registered_commands()
        
        try:
            self.start()
            
            # it seems that calling self._shutdown.wait() here prevents the
            # signal handler from executing
            while not self._shutdown.is_set():
                self._shutdown.wait(.1)
        except:
            self.logger.error('error', exc_info=1)
            self.shutdown()
        
        self.logger.info('Shutdown...')

def err(s):
    print '\033[91m%s\033[0m' % s
    sys.exit(1)

def load_config(config):
    config_module, config_obj = config.rsplit('.', 1)
    try:
        __import__(config_module)
        mod = sys.modules[config_module]
    except ImportError:
        err('Unable to import "%s"' % config_module)
    
    try:
        config = getattr(mod, config_obj)
    except AttributeError:
        err('Missing module-level "%s"' % config_obj)
    
    return config

if __name__ == '__main__':
    args = sys.argv[1:]
    
    if len(args) == 0:
        parser.print_help()
        err('Error, missing required parameter config.module')
        sys.exit(1)
    
    config = load_config(args[0])
    
    queue = config.QUEUE
    result_store = config.RESULT_STORE
    invoker = Invoker(queue, result_store)
    
    consumer = Consumer(invoker, config)
    consumer.run()