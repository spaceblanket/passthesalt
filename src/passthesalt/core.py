"""
The core PassTheSalt module.
"""

import re
from hmac import compare_digest

import serde
from serde import Model, field

from passthesalt.crypto import decrypt, encrypt, generate, pbkdf2_hash
from passthesalt.error import ConfigurationError, ContextError, LabelError
from passthesalt.model import ModifiedModel


def version():
    """
    Return the current PassTheSalt version.

    Returns:
        str: the version.
    """
    from passthesalt import __version__
    return __version__


class Secret(ModifiedModel):
    """
    A base class for a secret.
    """

    @classmethod
    def from_dict(cls, d, strict=True):
        """
        Convert a dictionary to a Secret.

        Args:
            d (dict): a serialized version of this Model.
            strict (bool): if set to False then no exception will be raised when
                unknown dictionary keys are present.

        Returns:
            Secret: an instance of Secret.

        Raises:
            `~serde.error.DeserializationError`: when the Secret kind is not
                valid.
        """
        if cls is Secret:
            kind = d.pop('kind', None)

            try:
                return SECRETS[kind].from_dict(d, strict=strict)
            except KeyError:
                raise serde.error.DeserializationError(f'{kind!r} is not a valid Secret kind')

        return super().from_dict(d, strict=strict)

    def to_dict(self, dict=None):
        """
        Convert this Secret to a dictionary.

        Args:
            dict (type): the class of the deserialized dictionary. This defaults
                to an `OrderedDict` so that the fields will be returned in the
                order they were defined on the Model.

        Returns:
            dict: the Secret serialized as a dictionary.

        Raises:
            `~serde.error.SerializationError`: when the Secret has an unknown
                kind.
        """
        d = super().to_dict()

        try:
            d['kind'] = KINDS[self.__class__]
        except KeyError:
            raise serde.error.SerializationError(f'{self.__class__} has an unknown kind')

        return d

    def __getattr__(self, item):
        """
        Override the access of `_label` and `_pts` attributes.

        Raises:
            ContextError: if attributes `_label` or `_pts` are accessed outside
                of a PassTheSalt context.
        """
        if item in ('_label', '_pts'):
            raise ContextError('Secret is not in a PassTheSalt context')

        return self.__getattribute__(item)

    def display(self):
        """
        A display tuple for tabulating this secret.

        Returns:
            (str, str): the label and the kind.
        """
        return (self._label, KINDS[self.__class__].split('.')[0], self.modified)

    def add_context(self, label, pts):
        """
        Set the context for this Secret.

        Args:
            label (str): the label for this Secret.
            pts (PassTheSalt): the PassTheSalt store for this Secret.
        """
        self._label = label
        self._pts = pts

    def check_context(self):
        """
        Check whether this Secret has a context.
        """
        self._pts
        self._label

    def remove_context(self):
        """
        Remove the context for this Secret.
        """
        del self._label
        del self._pts

    def add(self):
        """
        Add this Secret to the PassTheSalt store.
        """
        self.check_context()

    def get(self):
        """
        Retrieve the value for this Secret.

        Returns:
            str: the secret value.
        """
        raise NotImplementedError()

    def remove(self):
        """
        Remove this Secret from the PassTheSalt store.
        """
        self.check_context()


class Algorithm(Model):
    """
    A secret generation algorithm.
    """

    version = field.Int(default=1)
    length = field.Int(required=False)


class Generatable(Secret):
    """
    A generatable Secret.
    """

    salt = field.Str()
    algorithm = field.Nested(Algorithm, default=Algorithm)

    def display(self):
        """
        A display tuple for this tabulating this secret.

        Returns:
            (str, str, str): the label, the kind, and the salt.
        """
        return super().display() + (self.salt,)

    def get(self):
        """
        Generate the secret value for this Secret.

        Returns:
            str: the secret value.
        """
        return generate(
            self.salt,
            self._pts.master_key,
            version=self.algorithm.version,
            length=self.algorithm.length
        )


class Login(Generatable):
    """
    An account login Secret.
    """

    domain = field.Domain()
    username = field.Str()
    iteration = field.Int(required=False)

    @property
    def salt(self):
        """
        The salt for this Generatable secret.

        Returns:
            str: the salt.
        """
        return '|'.join((self.domain, self.username, str(self.iteration or 0)))


class Encrypted(Secret):
    """
    Represents and defines an encrypted Secret.
    """

    def __init__(self, secret, *args, **kwargs):
        """
        Create a new Encrypted.

        Args:
            secret (str): the secret to encrypt.
        """
        super().__init__(*args, **kwargs)
        self.secret = secret

    def _encrypt(self, secrets):
        """
        Encrypt a store with the master key.

        Args:
            secrets (dict): the store.
        """
        if not secrets:
            self._pts.secrets_encrypted = None
        else:
            self._pts.secrets_encrypted = encrypt(secrets, self._pts.master_key)

    def _decrypt(self):
        """
        Decrypt the encrypted store with the master key.

        Returns:
            dict: the encrypted store.
        """
        if not self._pts.secrets_encrypted:
            return {}

        return decrypt(self._pts.secrets_encrypted, self._pts.master_key)

    def add(self):
        """
        Add this Secret to the PassTheSalt store.
        """
        secrets = self._decrypt()
        secrets[self._label] = self.secret
        del self.secret
        self._encrypt(secrets)
        super().add()

    def get(self):
        """
        Decrypt secret for this Secret.

        Returns:
            str: the decrypted secret.
        """
        try:
            return self._decrypt()[self._label]
        except KeyError:
            raise LabelError(f'{self._label!r} does not exist in the encrypted store')

    def remove(self):
        """
        Remove this Secret from the PassTheSalt store.
        """
        secrets = self._decrypt()
        del secrets[self._label]
        self._encrypt(secrets)
        super().remove()


SECRETS = {
    'encrypted': Encrypted,
    'generatable': Generatable,
    'generatable.login': Login
}

KINDS = {cls: kind for kind, cls in SECRETS.items()}


class Master(Model):
    """
    Represents and defines a master password.
    """

    salt = field.Str()
    hash = field.Str()

    def __init__(self, master):
        """
        Configure master validation with the given master.

        Args:
            master (str): the master password.
        """
        salt, hash = pbkdf2_hash(master)
        super().__init__(salt=salt, hash=hash)

    def is_valid(self, master):
        """
        Check the given master with the stored hash.

        Args:
            master (str): the master password.

        Returns:
            bool: whether the master matches.
        """
        return compare_digest(self.hash, pbkdf2_hash(master, self.salt)[1])


class Config(Model):
    """
    Represents and defines config for PassTheSalt.
    """

    owner = field.Str(required=False)
    master = field.Nested(Master, required=False)


class PassTheSalt(ModifiedModel):
    """
    An object to store and manage Secrets.

    A PassTheSalt represents and defines a deterministic password generation and
    password storage system.
    """

    config = field.Nested(Config, required=False, default=Config)
    secrets = field.Dict(field.Str, Secret, required=False, default=dict)
    secrets_encrypted = field.Str(required=False)
    version = field.Str(required=False, default=version)

    def __init__(self, *args, **kwargs):
        """
        Create a new PassTheSalt.
        """
        super().__init__(*args, **kwargs)
        self._master = None
        self.version = version()

    @classmethod
    def from_dict(cls, d, strict=True):
        """
        Create a PassTheSalt object from a dictionary.

        Args:
            d (dict): the input dictionary.
            strict (bool): if set to False then no exception will be raised when
                unknown dictionary keys are present.

        Returns:
            PassTheSalt: a new PassTheSalt object.
        """
        pts = super().from_dict(d, strict=strict)

        # Add the current context to each Secret.
        for label, secret in pts.secrets.items():
            secret.add_context(label, pts)

        return pts

    def save(self, dict=None, **kwargs):
        """
        Write this PassTheSalt store to the configured path.

        Args:
            dict (type): the class of the deserialized dictionary. This defaults
                to an `OrderedDict` so that the fields will be returned in the
                order they were defined on the Model.
            **kwargs: extra keyword arguments passed directly to `json.dumps()`.
        """
        self.to_path(self.path, dict=dict, **kwargs)

    def with_master(self, master):
        """
        Configure PassTheSalt with a master password.

        Args:
            master: the master password for generating and encrypting secrets.
                This can be a callback for getting the password (for example
                through user input), or the actual master password as a string.

        Returns:
            PassTheSalt: this object.
        """
        self._master = master
        return self

    def with_path(self, path):
        """
        Configure PassTheSalt with a default path.

        Args:
            path (str): the default path to read and write to.

        Returns:
            PassTheSalt: this object.
        """
        self._path = path
        return self

    @property
    def master_key(self):
        """
        Return the master key.

        This is  constructed from the master password and the configured owner.

        Returns:
            str: the master key.
        """
        if self._master is None:
            raise ConfigurationError('no master password is configured')

        if callable(self._master):
            self._master = self._master(self)

        key = []

        if self.config.owner:
            key.append(self.config.owner)

        key.append(self._master)

        return '|'.join(key)

    @property
    def path(self):
        """
        Return the configured path if it is set.

        Returns:
            str: the configured path.

        Raises:
            `ConfigurationError`: when there is no configured path.
        """
        try:
            return self._path
        except AttributeError:
            raise ConfigurationError('no default path is configured')

    def labels(self, pattern=None, ignorecase=True):
        """
        Return the list of labels for secrets.

        This list can be optionally filtered with a regex pattern.

        Args:
            pattern (str): filter labels with a regex pattern.

        Returns:
            list: a list of labels matching the given pattern and prefix.

        Raises:
            LabelError: when the given pattern is an invalid regex expression.
        """
        labels = self.secrets.keys()

        if pattern:
            try:
                regex = re.compile(pattern)
            except re.error:
                raise LabelError(f'{pattern!r} is an invalid regex expression')

            labels = filter(regex.match, labels)

        return list(labels)

    def resolve(self, pattern):
        """
        Resolve a pattern and prefix to a single label.

        Args:
            pattern (str): filter labels with a regex pattern.

        Returns:
            str: the actual label of the secret.

        Raises:
            LabelError: if the pattern does not match any labels or multiple
                labels are matched.
        """
        if self.contains(pattern):
            return pattern

        matches = self.labels(pattern=pattern)

        if len(matches) == 1:
            return matches[0]
        elif not matches:
            raise LabelError(f'unable to resolve pattern {pattern!r}')
        else:
            raise LabelError(f'pattern {pattern!r} matches multiple secrets')

    def contains(self, label):
        """
        Whether the label exists.

        Args:
            label (str): the label for the secret.

        Returns:
            bool: True if the label exists else False.
        """
        return label in self.secrets

    def add(self, label, secret):
        """
        Add a secret to PassTheSalt.

        Args:
            label (str): the label for the secret.
            secret (Secret): the secret to add.
        """
        if self.contains(label):
            raise LabelError(f'{label!r} already exists')

        secret.add_context(label, self)
        secret.add()
        self.secrets[label] = secret
        self.touch()

    def get(self, label):
        """
        Retrieve a secret.

        Args:
            label (str): the label for the secret.

        Returns:
            Secret: the secret corresponding to the label.
        """
        return self.secrets[label]

    def pop(self, label):
        """
        Remove a secret and return the removed secret.

        Args:
            label (str): the label for the secret.

        Returns:
            Secret: the secret corresponding to the label.
        """
        try:
            secret = self.secrets.pop(label)
        except KeyError:
            raise LabelError(f'{label!r} does not exist')

        secret.remove()
        secret.remove_context()
        self.touch()

        return secret

    def remove(self, label):
        """
        Remove a secret.

        Args:
            label (str): the label for the secret.
        """
        self.pop(label)

    def move(self, label, new_label):
        """
        Rename a secret.

        Args:
            label (str): the label for the secret.
            new_label (str): the new label for the secret.
        """
        if self.contains(new_label):
            raise LabelError(f'{new_label!r} already exists')

        self.add(new_label, self.pop(label))