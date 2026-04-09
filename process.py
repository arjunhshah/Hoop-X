# from multiprocessing import Process
from threading import Thread
from .server import run, closeserver


job = Thread(target=run, kwargs={"port_range": (61863, 61873)}, daemon=True)


def register():
    job.start()


def unregister():
    closeserver()
