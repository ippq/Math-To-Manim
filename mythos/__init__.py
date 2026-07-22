"""Math-To-Manim: ask a question -> get one companion image or short GIF.

A minimal fork focused on illustrating knowledge atoms: one reasoning stage
picks a medium and specifies what goes on screen, then codegen turns that
into a Manim scene.

- ``mythos.harness``: the chain runner (visual-brief -> codegen -> verify ->
  render -> repair)
- ``mythos.charter``: the Cinematic Charter and shared parsing utilities
- ``mythos.backends``: the Claude CLI model backend
- ``mythos.gifs``: mp4 -> GIF conversion, and PNG lookup for image-medium runs
- ``mythos.cli``: the ``math-to-manim`` command
"""

__version__ = "1.1.0"
