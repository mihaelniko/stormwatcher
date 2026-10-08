from .cli import main

# Guarded: the engine process is started with multiprocessing "spawn", which
# re-imports the main module; it must not start a second UI.
if __name__ == "__main__":
    main()
