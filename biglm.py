import torch
from torch import nn
import torch.nn.functional as F

from utils import gelu, LayerNorm
from transformer_postln import TransformerLayer, Embedding, LearnedPositionalEmbedding, SelfAttentionMask
# more than 12 layers
#from transformer_preln import TransformerLayer, Embedding, LearnedPositionalEmbedding, SelfAttentionMask
from label_smoothing import LabelSmoothing 

class BIGLM(nn.Module):
    def __init__(self, local_rank, vocab, embed_dim, ff_embed_dim, num_heads, dropout, layers, smoothing_factor, approx):
        super(BIGLM, self).__init__()
        self.vocab = vocab
        self.embed_dim = embed_dim

        self.tok_embed = Embedding(self.vocab.size, embed_dim, self.vocab.padding_idx)
        self.pos_embed = LearnedPositionalEmbedding(embed_dim, device=local_rank)
        
        self.layers = nn.ModuleList()
        for i in range(layers):
            self.layers.append(TransformerLayer(embed_dim, ff_embed_dim, num_heads, dropout))
        self.emb_layer_norm = LayerNorm(embed_dim)
        self.one_more = nn.Linear(embed_dim, embed_dim)
        self.one_more_layer_norm = LayerNorm(embed_dim)
        self.out_proj = nn.Linear(embed_dim, self.vocab.size)
        
        self.attn_mask = SelfAttentionMask(device=local_rank)
        self.smoothing = LabelSmoothing(local_rank, self.vocab.size, self.vocab.padding_idx, smoothing_factor)
       
        self.dropout = dropout
        self.device = local_rank

        if approx == "none":
            self.approx = None
        elif approx == "adaptive":
            self.approx = nn.AdaptiveLogSoftmaxWithLoss(self.embed_dim, self.vocab.size, [10000, 20000, 200000])
        else:
            raise NotImplementedError("%s has not been implemented"%approx)
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.constant_(self.one_more.bias, 0.)
        nn.init.normal_(self.one_more.weight, std=0.02)
        nn.init.constant_(self.out_proj.bias, 0.)
        nn.init.normal_(self.out_proj.weight, std=0.02)
    
    def label_smotthing_loss(self, y_pred, y, y_mask, avg=True):
        seq_len, bsz = y.size()

        y_pred = torch.log(y_pred.clamp(min=1e-8))
        loss = self.smoothing(y_pred.view(seq_len * bsz, -1), y.view(seq_len * bsz, -1))
        if avg:
            return loss / torch.sum(y_mask)
        else:
            return loss / bsz

    def nll_loss(self, y_pred, y, y_mask, avg=True):
        cost = -torch.log(torch.gather(y_pred, 2, y.view(y.size(0), y.size(1), 1)))
        cost = cost.view(y.shape)
        y_mask = y_mask.view(y.shape)
        if avg:
            cost = torch.sum(cost * y_mask, 0) / torch.sum(y_mask, 0)
        else:
            cost = torch.sum(cost * y_mask, 0)
        cost = cost.view((y.size(1), -1))
        ppl = 2 ** cost
        return cost.sum().item(), ppl.sum().item()

    def ppl(self, truth, inp, msk):
        seq_len, bsz = inp.size()
        self_attn_mask = self.attn_mask(seq_len)
        x = self.tok_embed(inp) + self.pos_embed(inp)
        x = self.emb_layer_norm(x)
        padding_mask = torch.eq(truth, self.vocab.padding_idx)
        if not padding_mask.any():
            padding_mask = None
        for layer in self.layers:
            x, _ ,_ = layer(x, self_padding_mask=padding_mask, self_attn_mask = self_attn_mask)

        x = self.one_more_layer_norm(gelu(self.one_more(x)))
        pred = torch.softmax(self.out_proj(x), -1)
        _, pred_y = pred.max(-1)
        tot_tokens = msk.float().sum().item()
        acc = (torch.eq(pred_y, truth).float()*msk).sum().item() 
        nll, ppl = self.nll_loss(pred, truth, msk) 
        return acc, nll, ppl, tot_tokens, bsz
    
    def work(self, inp):
        seq_len, bsz = inp.size()
        self_attn_mask = self.attn_mask(seq_len)
        x = self.tok_embed(inp) + self.pos_embed(inp)
        x = self.emb_layer_norm(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        padding_mask = torch.eq(inp, self.vocab.padding_idx)
        if not padding_mask.any():
            padding_mask = None
        for layer in self.layers:
            x, _ ,_ = layer(x, self_padding_mask=padding_mask, self_attn_mask = self_attn_mask)

        x = self.one_more_layer_norm(gelu(self.one_more(x)))
        probs = torch.softmax(self.out_proj(x), -1)

        _, pred_y = probs.max(-1)
        
        return probs, pred_y
    
    def work_incremental(self, inp, incremental_state=None):
        seq_len, bsz = inp.size()
        x = self.tok_embed(inp) + self.pos_embed(inp)
        x = self.emb_layer_norm(x)
        padding_mask = torch.eq(inp, self.vocab.padding_idx)
        if not padding_mask.any():
            padding_mask = None

        if incremental_state is None:
            self_attn_mask = self.attn_mask(seq_len)
            incremental_state = {}
        else:
            x = x[-1, :, :].unsqueeze(0)
            self_attn_mask = None

        for layer in self.layers:
            x, _ ,_ = layer.work_incremental(x, self_padding_mask=padding_mask, self_attn_mask=self_attn_mask, incremental_state=incremental_state)

        x = self.one_more_layer_norm(gelu(self.one_more(x)))
        probs = torch.softmax(self.out_proj(x), -1)

        _, pred_y = probs.max(-1)
        return probs, pred_y, incremental_state
    

    def forward(self, truth, inp, msk):
        seq_len, bsz = inp.size()
        self_attn_mask = self.attn_mask(seq_len)
        x = self.tok_embed(inp) + self.pos_embed(inp)
        x = self.emb_layer_norm(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        padding_mask = torch.eq(truth, self.vocab.padding_idx)
        if not padding_mask.any():
            padding_mask = None
        for layer in self.layers:
            x, _ ,_ = layer(x, self_padding_mask=padding_mask, self_attn_mask = self_attn_mask)

        x = self.one_more_layer_norm(gelu(self.one_more(x)))
        pred = torch.softmax(self.out_proj(x), -1)

        loss = self.label_smotthing_loss(pred, truth, msk)
        
        _, pred_y = pred.max(-1)
        tot_tokens = msk.float().sum().item()
        acc = (torch.eq(pred_y, truth).float()*msk).sum().item() 
        nll, ppl = self.nll_loss(pred, truth, msk) 
        return (pred_y, truth), loss, acc, nll, ppl, tot_tokens, bsz
