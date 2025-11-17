import logging

class ClassNameFilter(logging.Filter):
    """
    Ajoute un attribut `class_name` au LogRecord, en essayant de retrouver
    la classe (self ou cls) depuis la pile d'appel au moment du logging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Valeur par défaut si aucune classe n'est trouvée
        record.class_name = "<no-class>"

        try:
            frame = inspect.currentframe()
            # Remonte la pile jusqu'à trouver la fonction qui correspond au log
            while frame:
                code = frame.f_code
                # On se contente de matcher sur le nom de la fonction
                if code.co_name == record.funcName:
                    local_self = frame.f_locals.get("self")
                    if local_self is not None:
                        record.class_name = type(local_self).__name__
                        break

                    local_cls = frame.f_locals.get("cls")
                    if isinstance(local_cls, type):
                        record.class_name = local_cls.__name__
                        break

                frame = frame.f_back

        except Exception:
            # En cas de souci, on laisse <no-class>
            pass

        return True


class CallerFormatter(logging.Formatter):
    """
    Formatter qui affiche:
    - niveau
    - temps
    - fichier et numéro de ligne
    - classe et fonction
    - message
    """

    default_format = (
        "[%(levelname)s] %(asctime)s "
        "%(filename)s:%(lineno)d "
        "%(class_name)s.%(funcName)s : "
        "%(message)s"
    )

    def __init__(self, fmt: str | None = None, datefmt: str | None = None):
        if fmt is None:
            fmt = self.default_format
        super().__init__(fmt=fmt, datefmt=datefmt, style='%' )


def init_logging() -> None:
    """Initialize logging configuration for the application."""
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

    # Ajout du filter qui découvre la classe
    console_handler.addFilter(ClassNameFilter())

    # Ajout du formatter custom
    formatter = CallerFormatter()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)