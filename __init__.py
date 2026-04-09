import bpy
modules = (
    "process",
)

reg, unreg = bpy.utils.register_submodule_factory(__package__, modules)


def register():
    reg()


def unregister():
    unreg()
