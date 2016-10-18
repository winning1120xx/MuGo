from io import BytesIO
import itertools
import gzip
import snappy
import numpy as np
import os
import struct
import time

from features import DEFAULT_FEATURES
import go
import sgf_wrapper
import utils

from contextlib import contextmanager

@contextmanager
def timer():
    tick = time.time()
    yield
    tock = time.time()
    print("%.2f\t" % (tock - tick), end='|')




# Number of data points to store in a chunk on disk
CHUNK_SIZE = 4096
CHUNK_HEADER_FORMAT = "iii?"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FORMAT)

def take_n(n, iterator):
    result = []
    try:
        for i in range(n):
            result.append(next(iterator))
    except StopIteration:
        pass
    finally:
        return result

def iter_chunks(chunk_size, iterator):
    while True:
        next_chunk = take_n(chunk_size, iterator)
        # If len(iterable) % chunk_size == 0, don't return an empty chunk.
        if next_chunk:
            yield next_chunk
        else:
            break

def make_onehot(dense_labels, num_classes):
    dense_labels = np.fromiter(dense_labels, dtype=np.int16)
    num_labels = dense_labels.shape[0]
    index_offset = np.arange(num_labels) * num_classes
    labels_one_hot = np.zeros((num_labels, num_classes), dtype=np.int16)
    labels_one_hot.flat[index_offset + dense_labels.ravel()] = 1
    return labels_one_hot

def find_sgf_files(*dataset_dirs):
    for dataset_dir in dataset_dirs:
        full_dir = os.path.join(os.getcwd(), dataset_dir)
        dataset_files = [os.path.join(full_dir, name) for name in os.listdir(full_dir)]
        for f in dataset_files:
            if os.path.isfile(f) and f.endswith(".sgf"):
                yield f

def get_positions_from_sgf(file):
    with open(file) as f:
        sgf = sgf_wrapper.SgfWrapper(f.read())
        for position_w_context in sgf.get_main_branch():
            if position_w_context.is_usable():
                yield position_w_context

def extract_features(positions):
    num_positions = len(positions)
    output = np.zeros([num_positions, go.N, go.N, DEFAULT_FEATURES.planes], dtype=np.float32)
    for i, pos in enumerate(positions):
        output[i] = DEFAULT_FEATURES.extract(pos)
    return output

def split_test_training(positions_w_context, est_num_positions):
    desired_test_size = 10**5
    if est_num_positions < 2 * desired_test_size:
        positions_w_context = list(positions_w_context)
        test_size = len(positions_w_context) // 3
        return positions_w_context[:test_size], [positions_w_context[test_size:]]
    else:
        test_chunk = take_n(desired_test_size, positions_w_context)
        training_chunks = iter_chunks(CHUNK_SIZE, positions_w_context)
        return test_chunk, training_chunks

def nopack(nparray):
    return nparray.tostring()

def halfpack(nparray):
    return (nparray == 1).tostring()

def fullpack(nparray):
    return np.packbits(nparray == 1).tostring()


class DataSet(object):
    def __init__(self, pos_features, next_moves, results, is_test=False):
        self.pos_features = pos_features
        self.next_moves = next_moves
        self.results = results
        self.is_test = is_test
        assert pos_features.shape[0] == next_moves.shape[0], "Didn't pass in same number of pos_features and next_moves."
        self.data_size = pos_features.shape[0]
        self.board_size = pos_features.shape[1]
        self.input_planes = pos_features.shape[-1]
        self._index_within_epoch = 0

    def get_batch(self, batch_size):
        assert batch_size < self.data_size
        if self._index_within_epoch + batch_size > self.data_size:
            # Shuffle the data and start over
            perm = np.arange(self.data_size)
            np.random.shuffle(perm)
            self.pos_features = self.pos_features[perm]
            self.next_moves = self.next_moves[perm]
            self._index_within_epoch = 0
        start = self._index_within_epoch
        end = start + batch_size
        self._index_within_epoch += batch_size
        return self.pos_features[start:end], self.next_moves[start:end]

    @staticmethod
    def from_positions_w_context(positions_w_context, is_test=False):
        positions, next_moves, results = zip(*positions_w_context)
        extracted_features = extract_features(positions)
        encoded_moves = make_onehot(map(utils.flatten_coords, next_moves), go.N ** 2)
        return DataSet(extracted_features, encoded_moves, results, is_test=is_test)

    def write(self, filename, compression, packing):
        header_bytes = struct.pack(CHUNK_HEADER_FORMAT, self.data_size, self.board_size, self.input_planes, self.is_test)
        pack_strategy = {
            'none': nopack,
            'half': halfpack,
            'full': fullpack,
        }[packing]
        with timer():
            position_bytes = pack_strategy(self.pos_features)
            next_move_bytes = pack_strategy(self.next_moves)

        with timer():
            if compression == 'none':
                with open(filename, 'wb') as f:
                    f.write(header_bytes)
                    f.write(position_bytes)
                    f.write(next_move_bytes)
            elif compression in ['gzip6', 'gzip9']:
                level = 6 if compression == 'gzip6' else 9
                with gzip.open(filename, 'wb', compresslevel=level) as f:
                    f.write(header_bytes)
                    f.write(position_bytes)
                    f.write(next_move_bytes)
            elif compression == 'snappy':
                with open(filename, 'wb') as f:
                    file_str = BytesIO()
                    file_str.write(header_bytes)
                    file_str.write(position_bytes)
                    file_str.write(next_move_bytes)
                    file_str.seek(0)
                    snappy.stream_compress(file_str, f)

    @staticmethod
    def read(filename, compression, packing):
        if compression == 'none':
            f = open(filename, 'rb')
        elif compression in ['gzip6', 'gzip9']:
            f = gzip.open(filename, 'rb')
        elif compression == 'snappy':
            with open(filename, 'rb') as real_f:
                f = BytesIO()
                snappy.stream_decompress(real_f, f)
                f.seek(0)

        header_bytes = f.read(CHUNK_HEADER_SIZE)
        data_size, board_size, input_planes, is_test = struct.unpack(CHUNK_HEADER_FORMAT, header_bytes)

        position_dims = data_size * board_size * board_size * input_planes
        next_move_dims = data_size * board_size * board_size

        if packing == 'none':
            flat_position = np.fromstring(f.read(position_dims * 4), dtype=np.float32)
            flat_nextmoves = np.fromstring(f.read(next_move_dims * 2), dtype=np.int16)
        elif packing == 'half':
            flat_position = np.fromstring(f.read(position_dims), dtype=np.uint8).astype(dtype=np.float32)
            flat_nextmoves = np.fromstring(f.read(next_move_dims), dtype=np.uint8).astype(dtype=np.int16)
        elif packing == 'full':
            # the +7 // 8 compensates for numpy's bitpacking padding
            packed_position_bytes = f.read((position_dims + 7) // 8)
            packed_next_move_bytes = f.read((next_move_dims + 7) // 8)
            flat_position = np.unpackbits(np.fromstring(packed_position_bytes, dtype=np.uint8))[:position_dims]
            flat_nextmoves = np.unpackbits(np.fromstring(packed_next_move_bytes, dtype=np.uint8))[:next_move_dims]

        # should have cleanly finished reading all bytes from file!
        assert len(f.read()) == 0
        f.close()

        pos_features = flat_position.reshape(data_size, board_size, board_size, input_planes)
        next_moves = flat_nextmoves.reshape(data_size, board_size * board_size)
        return DataSet(pos_features, next_moves, [], is_test=is_test)

def process_raw_data(*dataset_dirs, processed_dir="processed_data", **opts):
    sgf_files = list(find_sgf_files(*dataset_dirs))
    positions_w_context = itertools.chain(*map(get_positions_from_sgf, sgf_files))
    with timer():
        all_data = DataSet.from_positions_w_context(positions_w_context)
    test_filename = os.path.join(processed_dir, "all_data")
    all_data.write(test_filename, **opts)
