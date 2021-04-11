"""data.py - data loading"""


import os
import random
import logging
from pathlib import Path
from collections import OrderedDict
import pickle

import pytorch_lightning as pl
import torch
import numpy as np
from nussl import AudioSignal
import music_trees as mt
import tqdm
from tqdm.contrib.concurrent import process_map

import unicodedata
import re


def records2lists(records):
    keys = records[0].keys()
    lists = OrderedDict([(k, [r[k] for r in records]) for k in keys])

    return lists


def list2records(lists):
    keys = lists.keys()
    num_records = len(lists[keys[0]])
    records = []

    for i in range(num_records):
        record = {}
        for k in keys:
            record[k] = lists[k][i]
        records.append(record)

    return records


def slugify(value, allow_unicode=False):
    """
    Taken from https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize('NFKC', value)
    else:
        value = unicodedata.normalize('NFKD', value).encode(
            'ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '-', value).strip('-_')


class MetaDataset(torch.utils.data.Dataset):

    def __init__(self, name: str, partition: str, n_episodes: int, n_class: int, n_shot: int,
                 n_query: int, audio_tfm=None, epi_tfm=None, clear_cache=False, deterministic=False):
        """pytorch dataset for meta learning. 

        Args:
            name (str): name of the dataset. Must be a dirname under core.DATA_DIR
            partition (str): partition. One of 'train', or 'test', or 'validation', as defined in core.ASSETS_DIR / name / 'partition.json'
            n_class (int): number of classes in episode (n_way)
            n_shot (int): number of support examples per class
            n_query (int): number of query example
            transform ([type], optional): [description]. transform to apply. If none, returns an AudioSignal
        """
        super().__init__()
        # load the classlist for this partition
        self.root = mt.DATA_DIR / name
        self.files = self._load_files(name, partition)
        self.classes = sorted(list(self.files.keys()))

        self.n_episodes = n_episodes

        self.n_class = min(len(self.classes), n_class)
        self.n_shot = n_shot
        self.n_query = n_query

        self.audio_tfm = audio_tfm
        self.epi_tfm = epi_tfm

        cache_name = repr(self.audio_tfm)
        self.cache_root = mt.CACHE_DIR / name / cache_name
        self.cache_root.mkdir(exist_ok=True, parents=True)
        if self.cache_root.exists() and clear_cache:
            logging.warn(
                f'clear_cache = True and cache exists. clearing {self.cache_root}')
            os.remove(self.cache_root)

        self.cache_dataset()

        # generally, we want to create new episodes
        # on the fly
        # however, for validation and evaluation,
        # we want the episodes to remain deterministic
        # so we'll cache the episode metadata here
        self.deterministic = deterministic

        self.epi_cache_root = self.cache_root.parent /  \
            f'{cache_name}-deterministic-episodes-{partition}-k{n_shot}-c{n_class}-q{n_query}'
        self.epi_cache_root.mkdir(exist_ok=True)

    def __len__(self):
        return self.n_episodes

    def _load_files(self, name: str, partition: str):
        files = mt.utils.data.load_entry(mt.ASSETS_DIR / name / 'partition.json',
                                         format='json')[partition]
        # sort by key
        files = OrderedDict(sorted(files.items(), key=lambda x: x[0]))
        return files

    def cache_dataset(self):
        logging.info(f'caching dataset...')

        # go through all classnames
        for cl, records in self.files.items():
            process_map(self.cache_if_needed, records)
            # for i, entry in tqdm.tqdm(list(enumerate(records))):
            #     # all files belonging to a class
            #     self.cache_if_needed(entry)

    def cache_if_needed(self, entry: dict):
        entry_path = self.cache_root / entry['uuid']
        if entry_path.exists():
            with open(entry_path, 'rb') as f:
                cached_entry = pickle.load(f)

        if not entry_path.exists():
            cached_entry = self.transform_entry(entry)
            with open(entry_path, 'wb') as f:
                pickle.dump(cached_entry, f)

        cached_entry['audio_path'] = str(Path(
            mt.utils.data.get_path(cached_entry)).with_suffix('.wav').absolute())
        return cached_entry

    def transform_entry(self, entry: dict):
        # load audio
        audio_path = Path(mt.utils.data.get_path(entry)).with_suffix('.wav')
        signal = AudioSignal(path_to_input_file=str(
            audio_path)).to_mono(keep_dims=True)

        entry['audio'] = signal

        if self.audio_tfm is not None:
            entry = self.audio_tfm(entry)
        return entry

    def process_entry(self, entry: dict):
        if self.audio_tfm is None:
            return entry

        cached_entry = self.cache_if_needed(entry)
        return cached_entry

    def _get_example_for_class(self, name: str):
        # grab a random file
        entry = dict(random.choice(self.files[name]))
        return entry

    def _process_episode(self, episode):
        """apply process_item to all items in an episode"""
        episode['records'] = [self.process_entry(
            itm) for itm in episode['records']]

        # apply episode transform
        if self.epi_tfm is not None:
            episode = self.epi_tfm(episode)

        return episode

    def _episode_cache_get(self, index):
        return mt.utils.data.load_entry(self.epi_cache_root / str(index), format='json')

    def _episode_cache_set(self, index, item):
        mt.utils.data.save_entry(
            item, self.epi_cache_root / str(index), format='json')

    def _check_episode_cached(self, index):
        return Path(self.epi_cache_root / str(index)).exists()

    def generate_episode(self):
        """ generates an unprocessed episode"""
        subset = random.sample(self.classes, k=self.n_class)
        subset.sort()

        records = []

        metatypes = {'support': self.n_shot, 'query': self.n_query}
        for metatype, num_examples in metatypes.items():
            for label_idx, name in enumerate(subset):
                for meta_idx in range(num_examples):
                    item = self._get_example_for_class(name)
                    records.append(item)

        episode = {
            'n_class': len(subset),
            'n_shot': self.n_shot,
            'n_query': self.n_query,
            'classlist': subset,
            'records': records
        }
        return episode

    def __getitem__(self, index: int):
        """returns a dict with format:

        episode = {
            'support': List[AudioSignal], 
            'query': List[AudioSignal], 
            'classlist': List[str],
        }
        """
        if self.deterministic and self._check_episode_cached(index):
            # breakpoint()
            episode = self._episode_cache_get(index)
        else:
            episode = self.generate_episode()

            if self.deterministic:
                self._episode_cache_set(index, dict(episode))

        episode = self._process_episode(episode)

        return episode


class MetaDataModule(pl.LightningDataModule):
    """PyTorch Lightning data module

    Arguments
        name - string
            The name of the dataset
        batch_size - int
            The size of a batch
        num_workers - int or None
            Number data loading jobs to launch. If None, uses num cpu cores.
        **kwargs: 
            Any kwargs for MetaDataset. 
    """

    def __init__(self, name, n_shot: int, n_query: int, n_class: int,
                 batch_size=64, num_workers=None, **kwargs):
        super().__init__()
        self.name = name
        self.batch_size = batch_size
        self.num_workers = num_workers

        self.n_shot = n_shot
        self.n_query = n_query
        self.n_class = n_class

        self.kwargs = kwargs

    def setup(self, stage=None):
        # load all partitions
        partition = mt.utils.data.load_entry(
            mt.ASSETS_DIR / self.name / 'partition.json')

        if stage == 'fit':
            assert 'train' in partition
            self.dataset = MetaDataset(self.name, partition='train', deterministic=False,
                                       n_shot=self.n_shot, n_query=self.n_query, n_class=self.n_class,
                                       **self.kwargs)

            if 'val' in partition:
                self.val_dataset = MetaDataset(self.name, partition='val', deterministic=True,
                                               n_shot=self.n_shot, n_query=self.n_query, n_class=self.n_class,
                                               **self.kwargs)
            else:
                self.val_dataset = MetaDataset(self.name, partition='test', deterministic=True,
                                               n_shot=self.n_shot, n_query=self.n_query, n_class=self.n_class,
                                               **self.kwargs)

        if stage == 'test':
            self.test_dataset = MetaDataset(self.name, partition='test', deterministic=True,
                                            n_shot=self.n_shot, n_query=self.n_query, n_class=self.n_class,
                                            **self.kwargs)

    @staticmethod
    def add_argparse_args(parser):
        parser.add_argument('--dataset', type=str, required=True)
        parser.add_argument('--batch_size', type=int, default=1)
        parser.add_argument('--num_workers', type=int, required=False)
        parser.add_argument('--n_shot', type=int, default=4)
        parser.add_argument('--n_query', type=int, default=12)
        parser.add_argument('--n_class', type=int, default=12)

        return parser

    def train_dataloader(self):
        """Retrieve the PyTorch DataLoader for training"""
        return loader(self.dataset, 'train', self.batch_size, self.num_workers)

    def val_dataloader(self):
        """Retrieve the PyTorch DataLoader for validation"""
        assert hasattr(self, 'val_dataset')
        return loader(self.val_dataset, 'validation', self.batch_size, self.num_workers)

    def test_dataloader(self):
        """Retrieve the PyTorch DataLoader for testing"""
        assert hasattr(self, 'test_dataset')
        return loader(self.test_dataset, 'test', self.batch_size, self.num_workers)


def episode_collate(batch):
    # get the keys we expect
    keys = batch[0].keys()
    output = {}

    for key in keys:
        exmpl = batch[0][key]
        if isinstance(exmpl, np.ndarray):
            stack = np.stack([item[key] for item in batch])
            output[key] = torch.from_numpy(stack)
        elif isinstance(exmpl, torch.Tensor):
            output[key] = torch.stack([item[key] for item in batch])
        else:
            output[key] = [item[key] for item in batch]

    return output


def loader(dataset, partition, batch_size=64, num_workers=None):
    """Retrieve a data loader"""
    return torch.utils.data.DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        shuffle='train' in partition,
        num_workers=os.cpu_count() if num_workers is None else num_workers,
        pin_memory=True,
        collate_fn=episode_collate)
