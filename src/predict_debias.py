import numpy as np
from sklearn.metrics import roc_auc_score
from tqdm import tqdm
import torch
from torch.utils.data import Dataset, DataLoader
from os import path
import sys
import pandas as pd
from ast import literal_eval
import importlib
from multiprocessing import Pool
import time
import scipy.stats as ss


Model = getattr(importlib.import_module("model.NRMS"), "NRMS")
config = getattr(importlib.import_module('config'), "NRMSConfig")


device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


# def dcg_score(y_true, y_score, k=10):
#     order = np.argsort(y_score)[::-1]
#     y_true = np.take(y_true, order[:k])
#     gains = 2**y_true - 1
#     discounts = np.log2(np.arange(len(y_true)) + 2)
#     return np.sum(gains / discounts)


# def ndcg_score(y_true, y_score, k=10):
#     best = dcg_score(y_true, y_true, k)
#     actual = dcg_score(y_true, y_score, k)
#     return actual / best


# def mrr_score(y_true, y_score):
#     order = np.argsort(y_score)[::-1]
#     y_true = np.take(y_true, order)
#     rr_score = y_true / (np.arange(len(y_true)) + 1)
#     return np.sum(rr_score) / np.sum(y_true)


def value2rank(d):
    values = list(d.values())
    ranks = [sorted(values, reverse=True).index(x) for x in values]
    return {k: ranks[i] + 1 for i, k in enumerate(d.keys())}


class NewsDataset(Dataset):
    """
    Load news for evaluation.
    """
    def __init__(self, news_path):
        super(NewsDataset, self).__init__()
        self.news_parsed = pd.read_table(
            news_path,
            usecols=['id'] + config.dataset_attributes['news'],
            converters={
                attribute: literal_eval
                for attribute in set(config.dataset_attributes['news']) & set([
                    'title', 'abstract', 'title_entities', 'abstract_entities'
                ])
            })
        self.news2dict = self.news_parsed.to_dict('index')
        for key1 in self.news2dict.keys():
            for key2 in self.news2dict[key1].keys():
                if type(self.news2dict[key1][key2]) != str:
                    self.news2dict[key1][key2] = torch.tensor(
                        self.news2dict[key1][key2])

    def __len__(self):
        return len(self.news_parsed)

    def __getitem__(self, idx):
        item = self.news2dict[idx]
        return item


class UserDataset(Dataset):
    """
    Load users for evaluation, duplicated rows will be dropped
    """
    def __init__(self, behaviors_path, user2int_path):
        super(UserDataset, self).__init__()
        self.behaviors = pd.read_table(behaviors_path,
                                       header=None,
                                       usecols=[1, 3],
                                       names=['user', 'clicked_news'])
        self.behaviors.clicked_news.fillna(' ', inplace=True)
        self.behaviors.drop_duplicates(inplace=True)
        user2int = dict(pd.read_table(user2int_path).values.tolist())
        user_total = 0
        user_missed = 0
        for row in self.behaviors.itertuples():
            user_total += 1
            if row.user in user2int:
                self.behaviors.at[row.Index, 'user'] = user2int[row.user]
            else:
                user_missed += 1
                self.behaviors.at[row.Index, 'user'] = 0

    def __len__(self):
        return len(self.behaviors)

    def __getitem__(self, idx):
        row = self.behaviors.iloc[idx]
        item = {
            "user":
            row.user,
            "clicked_news_string":
            row.clicked_news,
            "clicked_news":
            row.clicked_news.split()[:config.num_clicked_news_a_user]
        }
        item['clicked_news_length'] = len(item["clicked_news"])
        repeated_times = config.num_clicked_news_a_user - len(
            item["clicked_news"])
        assert repeated_times >= 0
        item["clicked_news"] = ['PADDED_NEWS'
                                ] * repeated_times + item["clicked_news"]

        return item


class BehaviorsDataset(Dataset):
    """
    Load behaviors for evaluation, (user, time) pair as session
    """
    def __init__(self, behaviors_path):
        super(BehaviorsDataset, self).__init__()
        self.behaviors = pd.read_table(behaviors_path,
                                       header=None,
                                       usecols=range(5),
                                       names=[
                                           'impression_id', 'user', 'time',
                                           'clicked_news', 'impressions'
                                       ])
        self.behaviors.clicked_news.fillna(' ', inplace=True)
        self.behaviors.impressions = self.behaviors.impressions.str.split()

    def __len__(self):
        return len(self.behaviors)

    def __getitem__(self, idx):
        row = self.behaviors.iloc[idx]
        item = {
            "impression_id": row.impression_id,
            "user": row.user,
            "time": row.time,
            "clicked_news_string": row.clicked_news,
            "impressions": row.impressions
        }
        return item


# def calculate_single_user_metric(pair):
#     try:
#         auc = roc_auc_score(*pair)
#         mrr = mrr_score(*pair)
#         ndcg5 = ndcg_score(*pair, 5)
#         ndcg10 = ndcg_score(*pair, 10)
#         return [auc, mrr, ndcg5, ndcg10]
#     except ValueError:
#         return [np.nan] * 4

# Define the function to compute category means
def compute_category_means(embeddings, categories, categories_unique):
    means = {}
    for c in categories_unique:
        mask = (categories == c)
        means[c] = embeddings[mask].mean(dim=0)
    return means


# Define the function to construct matrix C
def construct_c(embeddings, categories, category_means, categories_unique):
    embedding_dim = embeddings.shape[1]
    C = torch.zeros((embedding_dim, embedding_dim), dtype=torch.float32).to(device)
    for c in categories_unique:
        mask = (categories == c)
        diffs = embeddings[mask] - category_means[c]
        for diff in diffs:
            C += torch.ger(diff, diff) / diffs.shape[0]
    return C


# Define the function to identify bias subspace
def identify_bias_subspace(C, k=2):
    U, S, V = torch.svd(C)
    return V[:, :k]


# Define the function to neutralize embeddings
def neutralize_embeddings(embeddings, bias_subspace):
    for i in range(bias_subspace.shape[1]):
        component = bias_subspace[:, i].unsqueeze(1)
        projections = torch.matmul(embeddings, component) * component.T
        embeddings -= projections

        # Normalize each embedding after removing the projection
        embeddings_norm = torch.norm(embeddings, p=2, dim=1, keepdim=True)
        normalized_embeddings = embeddings / embeddings_norm
    return normalized_embeddings


@torch.no_grad()
def evaluate(model, directory, num_workers, max_count=sys.maxsize):
    """
    Evaluate model on target directory.
    Args:
        model: model to be evaluated
        directory: the directory that contains two files (behaviors.tsv, news_parsed.tsv)
        num_workers: processes number for calculating metrics
    Returns:
        AUC
        MRR
        nDCG@5
        nDCG@10
    """
    news_dataset = NewsDataset(path.join(directory, 'news_parsed.tsv'))
    news_dataloader = DataLoader(news_dataset,
                                 batch_size=config.batch_size * 16,
                                 shuffle=False,
                                 num_workers=config.num_workers,
                                 drop_last=False,
                                 pin_memory=True)

    news2vector = {}
    for minibatch in tqdm(news_dataloader,
                          desc="Calculating vectors for news"):
        news_ids = minibatch["id"]
        if any(id not in news2vector for id in news_ids):
            news_vector = model.get_news_vector(minibatch)
            for id, vector in zip(news_ids, news_vector):
                if id not in news2vector:
                    news2vector[id] = vector

    news_category = pd.read_table(path.join(directory, 'news_parsed.tsv'),
                                  usecols=['id', 'category'])
    
    categories_unique = news_category['category'].unique()
    news_ids = news_category['id'].values
    categories = news_category['category'].values
    embeddings_tensor = torch.stack([news2vector[nid] for nid in news_ids])
    categories_tensor = torch.tensor(categories, dtype=torch.long)

    # Compute means
    category_means = compute_category_means(embeddings_tensor, categories_tensor, categories_unique)

    # Construct matrix C
    C = construct_c(embeddings_tensor, categories_tensor, category_means, categories_unique)

    # Identify bias subspace
    bias_subspace = identify_bias_subspace(C)

    # Neutralize embeddings
    neutralized_embeddings_tensor = neutralize_embeddings(embeddings_tensor, bias_subspace)

    news2vector = {nid: emb.to(device) for nid, emb in zip(news_ids, neutralized_embeddings_tensor)}
    
    news2vector['PADDED_NEWS'] = torch.zeros(
        list(news2vector.values())[0].size())

    user_dataset = UserDataset(path.join(directory, 'behaviors.tsv'),
                               './data/train/user2int.tsv')
    user_dataloader = DataLoader(user_dataset,
                                 batch_size=config.batch_size * 16,
                                 shuffle=False,
                                 num_workers=config.num_workers,
                                 drop_last=False,
                                 pin_memory=True)

    user2vector = {}
    for minibatch in tqdm(user_dataloader,
                          desc="Calculating vectors for users"):
        user_strings = minibatch["clicked_news_string"]
        if any(user_string not in user2vector for user_string in user_strings):
            clicked_news_vector = torch.stack([
                torch.stack([news2vector[x].to(device) for x in news_list],
                            dim=0) for news_list in minibatch["clicked_news"]
            ],
                                              dim=0).transpose(0, 1)
            user_vector = model.get_user_vector(clicked_news_vector)
            for user, vector in zip(user_strings, user_vector):
                if user not in user2vector:
                    user2vector[user] = vector

    behaviors_dataset = BehaviorsDataset(path.join(directory, 'behaviors.tsv'))
    behaviors_dataloader = DataLoader(behaviors_dataset,
                                      batch_size=1,
                                      shuffle=False,
                                      num_workers=config.num_workers)

    count = 0

    tasks = []

    out_file = open(path.join(directory, 'prediction.txt'), 'w')

    for minibatch in tqdm(behaviors_dataloader,
                          desc="Calculating probabilities"):
        count += 1
        if count == max_count:
            break

        candidate_news_vector = torch.stack([
            news2vector[news[0].split('-')[0]]
            for news in minibatch['impressions']
        ],
                                            dim=0)
        user_vector = user2vector[minibatch['clicked_news_string'][0]]
        impression_id = minibatch["impression_id"].item()
        click_probability = model.get_prediction(candidate_news_vector,
                                                 user_vector)

        y_pred_prob = click_probability.tolist()
        y_pred = len(y_pred_prob)-ss.rankdata(y_pred_prob, method='ordinal')+1
        # assert len(y_pred_prob) == len(minibatch['impressions'])
        # for i in range(len(y_pred_prob)):
        #     y_pred.append([y_pred_prob[i], minibatch['impressions'][i]])
        # y_pred = sorted(y_pred, key=lambda x: x[0], reverse=True)
        # y_pred = [x[1][0] for x in y_pred]
        y_pred = [str(int(x)) for x in y_pred]
        y_pred_str = ','.join(y_pred)
        out_file.write(f'{impression_id} [{y_pred_str}]\n')
    out_file.close()
    # with Pool(processes=num_workers) as pool:
    #     results = pool.map(calculate_single_user_metric, tasks)

    # aucs, mrrs, ndcg5s, ndcg10s = np.array(results).T
    # return np.nanmean(aucs), np.nanmean(mrrs), np.nanmean(ndcg5s), np.nanmean(
    #     ndcg10s)


if __name__ == '__main__':
    begin = time.time()
    print('Using device:', device)
    print('Evaluating NRMS')
    # Don't need to load pretrained word/entity/context embedding
    # since it will be loaded from checkpoint later
    try:
        pretrained_word_embedding = torch.from_numpy(
            np.load('./data/train/pretrained_word_embedding.npy')).float()
    except FileNotFoundError:
        pretrained_word_embedding = None
        
    model = Model(config, pretrained_word_embedding).to(device)
    from train import latest_checkpoint  # Avoid circular imports
    checkpoint_path = latest_checkpoint(path.join('./checkpoint', "NRMS"))
    if checkpoint_path is None:
        print('No checkpoint file found!')
        exit()
    print(f"Load saved parameters in {checkpoint_path}")
    checkpoint = torch.load(checkpoint_path)
    model.load_state_dict(checkpoint['model_state_dict'])
    model.eval()
    evaluate(model, './data/test', config.num_workers)
   
    print(f'Time of execution: {time.time()-begin} seconds.')