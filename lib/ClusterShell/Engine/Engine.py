#
# Copyright CEA/DAM/DIF (2007, 2008, 2009)
#  Contributor: Stephane THIELL <stephane.thiell@cea.fr>
#
# This file is part of the ClusterShell library.
#
# This software is governed by the CeCILL-C license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL-C
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".
#
# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.
#
# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.
#
# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL-C license and that you accept its terms.
#
# $Id$

"""
Interface of underlying Task's Engine.

An Engine implements a loop your thread enters and uses to call event handlers
in response to incoming events (from workers, timers, etc.).
"""

from sets import Set

import heapq
import thread
import time

class EngineException(Exception):
    """
    Base engine exception.
    """

class EngineAbortException(EngineException):
    """
    Raised on user abort.
    """

class EngineTimeoutException(EngineException):
    """
    Raised when a timeout is encountered.
    """

class EngineIllegalOperationError(EngineException):
    """
    Error raised when an illegal operation has been performed.
    """

class EngineAlreadyRunningError(EngineIllegalOperationError):
    """
    Error raised when the engine is already running.
    """


class EngineBaseTimer:
    """
    Abstract class for ClusterShell's engine timer. Such a timer
    requires a relative fire time (delay) in seconds (as float), and
    supports an optional repeating interval in seconds (as float too).

    See EngineTimer for more information about ClusterShell timers.
    """

    def __init__(self, fire_delay, interval=-1.0, autoclose=False):
        """
        Create a base timer.
        """
        self.fire_delay = fire_delay
        self.interval = interval
        self.autoclose = autoclose
        self._engine = None
        self._timercase = None

    def _set_engine(self, engine):
        """
        Bind to engine, called by Engine.
        """
        if self._engine:
            # A timer can be registered to only one engine at a time.
            raise EngineIllegalOperationError("Already bound to engine.")

        self._engine = engine

    def invalidate(self):
        """
        Invalidates a timer object, stopping it from ever firing again.
        """
        if self._engine:
            self._engine.timerq.invalidate(self)
            self._engine = None

    def is_valid(self):
        """
        Returns a boolean value that indicates whether an EngineTimer
        object is valid and able to fire.
        """
        return self._engine != None

    def set_nextfire(self, fire_delay, interval=-1):
        """
        Set the next firing delay in seconds for an EngineTimer object.

        The optional paramater `interval' sets the firing interval
        of the timer. If not specified, the timer fires once and then
        is automatically invalidated.

        Time values are expressed in second using floating point
        values. Precision is implementation (and system) dependent.

        It is safe to call this method from the task owning this
        timer object, in any event handlers, anywhere.

        However, resetting a timer's next firing time may be a
        relatively expensive operation. It is more efficient to let
        timers autorepeat or to use this method from the timer's own
        event handler callback (ie. from its ev_timer).
        """
        if not self.is_valid():
            raise EngineIllegalOperationError("Operation on invalid timer.")

        self.fire_delay = fire_delay
        self.interval = interval
        self._engine.timerq.reschedule(self)

    def _fire(self):
        raise NotImplementedError("Derived classes must implement.")


class EngineTimer(EngineBaseTimer):
    """
    Concrete class EngineTimer

    An EngineTimer object represents a timer bound to an engine that
    fires at a preset time in the future. Timers can fire either only
    once or repeatedly at fixed time intervals. Repeating timers can
    also have their next firing time manually adjusted.

    A timer is not a real-time mechanism; it fires when the task's
    underlying engine to which the timer has been added is running and
    able to check if the timer's firing time has passed.
    """

    def __init__(self, fire_delay, interval, autoclose, handler):
        EngineBaseTimer.__init__(self, fire_delay, interval, autoclose)
        self.eh = handler
        assert self.eh != None, "An event handler is needed for timer."

    def _fire(self):
        self.eh._invoke("ev_timer", self)

class _EngineTimerQ:

    class _EngineTimerCase:
        """
        Helper class that allows comparisons of fire times, to be easily used
        in an heapq.
        """
        def __init__(self, client):
            self.client = client
            self.client._timercase = self
            # arm timer (first time)
            assert self.client.fire_delay > 0
            self.fire_date = self.client.fire_delay + time.time()

        def __cmp__(self, other):
            if self.fire_date < other.fire_date:
                return -1
            elif self.fire_date > other.fire_date:
                return 1
            else:
                return 0

        def arm(self, client):
            assert client != None
            self.client = client
            self.client._timercase = self
            # setup next firing date
            time_current = time.time()
            if self.client.fire_delay > 0:
                self.fire_date = self.client.fire_delay + time_current
            else:
                interval = float(self.client.interval)
                assert interval > 0
                self.fire_date += interval
                # If the firing time is delayed so far that it passes one
                # or more of the scheduled firing times, reschedule the
                # timer for the next scheduled firing time in the future.
                while self.fire_date < time_current:
                    self.fire_date += interval

        def disarm(self):
            client = self.client
            client._timercase = None
            self.client = None
            return client

        def armed(self):
            return self.client != None
            

    def __init__(self, engine):
        """
        Initializer.
        """
        self._engine = engine
        self.timers = []
        self.armed_count = 0

    def __len__(self):
        """
        Return the number of active timers.
        """
        return self.armed_count

    def schedule(self, client):
        """
        Insert and arm a client's timer.
        """
        # arm only if fire is set
        if client.fire_delay > 0:
            heapq.heappush(self.timers, _EngineTimerQ._EngineTimerCase(client))
            self.armed_count += 1
            if not client.autoclose:
                self._engine.evlooprefcnt += 1

    def reschedule(self, client):
        """
        Re-insert client's timer.
        """
        if client._timercase:
            self.invalidate(client)
            self._dequeue_disarmed()
            self.schedule(client)

    def invalidate(self, client):
        """
        Invalidate client's timer. Current implementation doesn't really remove
        the timer, but simply flags it as disarmed.
        """
        if not client._timercase:
            # if timer is being fire, invalidate its values
            client.fire_delay = 0
            client.interval = 0
            return

        if self.armed_count <= 0:
            raise ValueError, "Engine client timer not found in timer queue"

        client._timercase.disarm()
        self.armed_count -= 1
        if not client.autoclose:
            self._engine.evlooprefcnt -= 1

    def _dequeue_disarmed(self):
        """
        Dequeue disarmed timers (sort of garbage collection).
        """
        while len(self.timers) > 0 and not self.timers[0].armed():
            heapq.heappop(self.timers)

    def fire(self):
        """
        Remove the smallest timer from the queue and fire its associated client.
        Raise IndexError if the queue is empty.
        """
        self._dequeue_disarmed()

        timercase = heapq.heappop(self.timers)
        client = timercase.disarm()
        
        client.fire_delay = 0
        client._fire()

        if client.fire_delay > 0 or client.interval > 0:
            timercase.arm(client)
            heapq.heappush(self.timers, timercase)
        else:
            self.armed_count -= 1
            if not client.autoclose:
                self._engine.evlooprefcnt -= 1

    def nextfire_delay(self):
        """
        Return next timer fire delay (relative time).
        """
        self._dequeue_disarmed()
        if len(self.timers) > 0:
            return max(0., self.timers[0].fire_date - time.time())

        return -1

    def expired(self):
        """
        Has a timer expired?
        """
        self._dequeue_disarmed()
        return len(self.timers) > 0 and \
            (self.timers[0].fire_date - time.time()) <= 1e-2

    def clear(self):
        """
        Stop and clear all timers.
        """
        for timer in self.timers:
            if timer.armed():
                timer.client.invalidate()

        self.timers = []
        self.armed_count = 0


class Engine:
    """
    Interface for ClusterShell engine. Subclasses have to implement a runloop
    listening for client events.
    """

    # Engine client I/O event interest bits
    E_READABLE = 0x1
    E_WRITABLE = 0x2
    E_ANY = 0x3

    def __init__(self, info):
        """
        Initialize base class.
        """
        # take a reference on info dict
        self.info = info

        # keep track of all clients
        self._clients = Set()

        # keep track of registered clients in a dict where keys are fileno
        # note: len(self.reg_clients) <= configured fanout
        self.reg_clients = {}

        # A boolean that indicates when reg_clients has changed, or when
        # some client interest event mask has changed. It is set by the
        # base class, and reset by each engine implementation.
        # Engines often deal with I/O events in chunk, and some event
        # may lead to change to some other "client interest event mask"
        # or could even register or close other clients. When such
        # changes are made, this boolean is set to True, allowing the
        # engine implementation to reconsider their events got by chunk.
        self.reg_clients_changed = False

        # timer queue to handle both timers and clients timeout
        self.timerq = _EngineTimerQ(self)

        # reference count to the event loop (must include registered
        # clients and timers configured WITHOUT autoclose)
        self.evlooprefcnt = 0

        # thread stuffs
        self.run_lock = thread.allocate_lock()
        self.start_lock = thread.allocate_lock()
        self.start_lock.acquire()

    def clients(self):
        """
        Get a copy of clients set.
        """
        return self._clients.copy()

    def add(self, client):
        """
        Add a client to engine. Subclasses that override this method
        should call base class method.
        """
        # bind to engine
        client._set_engine(self)

        # add to clients set
        self._clients.add(client)

        if self.run_lock.locked():
            # in-fly add if running
            self.register(client._start())

    def remove(self, client, did_timeout=False):
        """
        Remove a client from engine. Subclasses that override this
        method should call base class method.
        """
        self._debug("REMOVE %s" % client)
        self._clients.remove(client)

        if client.registered:
            self.unregister(client)
            client._close(force=False, timeout=did_timeout)
            self.start_all()

    def clear(self, did_timeout=False):
        """
        Remove all clients. Subclasses that override this method should
        call base class method.
        """
        while len(self._clients) > 0:
            client = self._clients.pop()
            if client.registered:
                self.unregister(client)
                client._close(force=True, timeout=did_timeout)

    def register(self, client):
        """
        Register a client. Subclasses that override this method should
        call base class method.
        """
        assert client in self._clients
        assert not client.registered

        rfd = client.reader_fileno()
        wfd = client.writer_fileno()
        assert rfd != None or wfd != None

        self._debug("REG %s(r%s,w%s)(autoclose=%s)" % (client.__class__.__name__,
            rfd, wfd, client.autoclose))

        client._events = 0
        client.registered = True

        if client.autoclose:
            refcnt_inc = 0
        else:
            refcnt_inc = 1

        if rfd != None:
            self.reg_clients[rfd] = client
            self.reg_clients_changed = True
            client._events |= Engine.E_READABLE
            self.evlooprefcnt += refcnt_inc
            self._register_specific(rfd, Engine.E_READABLE)
        if wfd != None:
            self.reg_clients[wfd] = client
            self.reg_clients_changed = True
            client._events |= Engine.E_WRITABLE
            self.evlooprefcnt += refcnt_inc
            self._register_specific(wfd, Engine.E_WRITABLE)

        client._new_events = client._events

        # start timeout timer
        self.timerq.schedule(client)

    def unregister_writer(self, client):
        self._debug("UNREG WRITER r%s,w%s" % (client.reader_fileno(), client.writer_fileno()))
        if client.autoclose:
            refcnt_inc = 0
        else:
            refcnt_inc = 1

        wfd = client.writer_fileno()
        if wfd != None:
            self._unregister_specific(wfd, client._events & Engine.E_WRITABLE)
            client._events &= ~Engine.E_WRITABLE
            del self.reg_clients[wfd]
            self.reg_clients_changed = True
            self.evlooprefcnt -= refcnt_inc

    def unregister(self, client):
        """
        Unregister a client. Subclasses that override this method should
        call base class method.
        """
        # sanity check
        assert client.registered
        self._debug("UNREG %s (r%s,w%s)" % (client.__class__.__name__,
            client.reader_fileno(), client.writer_fileno()))
        
        # remove timeout timer
        self.timerq.invalidate(client)
        
        if client.autoclose:
            refcnt_inc = 0
        else:
            refcnt_inc = 1
            
        # clear interest events
        rfd = client.reader_fileno()
        if rfd != None:
            self._unregister_specific(rfd, client._events & Engine.E_READABLE)
            client._events &= ~Engine.E_READABLE
            del self.reg_clients[rfd]
            self.reg_clients_changed = True
            self.evlooprefcnt -= refcnt_inc

        wfd = client.writer_fileno()
        if wfd != None:
            self._unregister_specific(wfd, client._events & Engine.E_WRITABLE)
            client._events &= ~Engine.E_WRITABLE
            del self.reg_clients[wfd]
            self.reg_clients_changed = True
            self.evlooprefcnt -= refcnt_inc

        client._new_events = 0
        client.registered = False

    def modify(self, client, set, clear):
        """
        Modify the next loop interest events bitset for a client.
        """
        self._debug("MODEV set:0x%x clear:0x%x %s" % (set, clear, client))
        client._new_events &= ~clear
        client._new_events |= set

        if not client._processing:
            # modifying a non processing client?
            self.reg_clients_has_changed = True
            # apply new_events now
            self.set_events(client, client._new_events)

    def set_events(self, client, new_events):
        """
        Set the active interest events bitset for a client.
        """
        assert not client._processing

        self._debug("SETEV new_events:0x%x events:0x%x %s" % (new_events,
            client._events, client))

        if client.autoclose:
            refcnt_inc = 0
        else:
            refcnt_inc = 1

        chgbits = new_events ^ client._events
        if chgbits == 0:
            return

        # configure interest events as appropriate
        rfd = client.reader_fileno()
        if rfd != None:
            if chgbits & Engine.E_READABLE:
                status = new_events & Engine.E_READABLE
                self._modify_specific(rfd, Engine.E_READABLE, status)
                if status:
                    client._events |= Engine.E_READABLE
                else:
                    client._events &= ~Engine.E_READABLE

        wfd = client.writer_fileno()
        if wfd != None:
            if chgbits & Engine.E_WRITABLE:
                status = new_events & Engine.E_WRITABLE
                self._modify_specific(wfd, Engine.E_WRITABLE, status)
                if status:
                    client._events |= Engine.E_WRITABLE
                else:
                    client._events &= ~Engine.E_WRITABLE

        client._new_events = client._events

    def add_timer(self, timer):
        """
        Add engine timer.
        """
        timer._set_engine(self)
        self.timerq.schedule(timer)

    def remove_timer(self, timer):
        """
        Remove engine timer.
        """
        self.timerq.invalidate(timer)

    def fire_timers(self):
        """
        Fire expired timers for processing.
        """
        while self.timerq.expired():
            self.timerq.fire()

    def start_all(self):
        """
        Start and register all possible clients, in respect of task fanout.
        """
        fanout = self.info["fanout"]
        assert fanout > 0
        if fanout <= len(self.reg_clients) + 1: # +1 for possible writer client
             return

        for client in self._clients:
            if not client.registered:
                self._debug("START CLIENT %s" % client.__class__.__name__)
                self.register(client._start())
                if fanout <= len(self.reg_clients):
                    break
    
    def run(self, timeout):
        """
        Run engine in calling thread.
        """
        # change to running state
        if not self.run_lock.acquire(0):
            raise EngineAlreadyRunningError()

        # start clients now
        self.start_all()

        # we're started
        self.start_lock.release()

        # note: try-except-finally not supported before python 2.5
        try:
            try:
                self.runloop(timeout)
            except Exception, e:
                # any exceptions invalidate clients
                self.clear(isinstance(e, EngineTimeoutException))
                raise
        finally:
            # cleanup
            self.timerq.clear()

            # change to idle state
            self.start_lock.acquire()
            self.run_lock.release()

    def runloop(self, timeout):
        """
        Engine specific run loop. Derived classes must implement.
        """
        raise NotImplementedError("Derived classes must implement.")

    def abort(self):
        """
        Abort task's running loop.
        """
        raise EngineAbortException()

    def exited(self):
        """
        Return True if the engine has exited the runloop once.
        """
        raise NotImplementedError("Derived classes must implement.")

    def join(self):
        """
        Block calling thread until runloop has finished.
        """
        # make sure engine has started first
        self.start_lock.acquire()
        self.start_lock.release()
        # joined once run_lock is available
        self.run_lock.acquire()
        self.run_lock.release()

    def _debug(self, s):
        # library engine debugging hook
        pass
