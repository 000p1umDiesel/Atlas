# Attention Is All You Need

## Introduction

The Transformer is a neural network architecture that relies entirely on an attention mechanism instead of recurrence. It was proposed in 2017 by Ashish Vaswani and colleagues at Google Brain. The model reached state-of-the-art quality on machine translation while being much faster to train than recurrent networks.

## Scaled Dot-Product Attention

Attention maps a query and a set of key-value pairs to an output. The weights are computed with a softmax over the scaled dot products of the query with all keys. Softmax turns the raw scores into a probability distribution, so the output is a weighted average of the values.

## Multi-Head Attention

Instead of a single attention function, the Transformer runs several attention heads in parallel. Each head learns to focus on different positions and representation subspaces, and the outputs are concatenated and projected.

## Impact

The Transformer became the foundation of large language models such as BERT and GPT. Retrieval-augmented generation systems also use Transformer encoders to embed documents.
